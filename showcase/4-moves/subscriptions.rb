# Renewal, upgrade, and cancellation rules for subscriptions.
#
# Every public method takes a subscriber and returns either a receipt
# or nil; billing failures raise BillingError so the caller can retry.
class Subscriptions
  GRACE_DAYS = 3
  MAX_PAUSE_DAYS = 90
  TRIAL_DAYS = 14

  class BillingError < StandardError; end

  attr_reader :plans, :billing, :receipts, :audit, :mailer

  def initialize(plans:, billing:, receipts:, audit:, mailer:)
    @plans = plans
    @billing = billing
    @receipts = receipts
    @audit = audit
    @mailer = mailer
  end

  # Renews a subscriber's plan for one more period.
  def renew(subscriber, at: Time.now)
    return if subscriber.cancelled? || subscriber.paused?

    plan = plans.fetch(subscriber.plan_id)
    account = subscriber.account
    grace = GRACE_DAYS * 86_400
    period = plan.period_after(subscriber.renewed_at, grace: grace)
    charge = billing.charge(account, plan.price, grace: grace)
    raise BillingError, charge.error unless charge.ok?

    subscriber.update!(renewed_at: at, period: period)
    audit.count(:renewals)
    audit.gauge(:period_days, period.days)
    receipts.build(subscriber, charge)
  end

  # Starts a free trial; the first charge happens when it ends.
  def start_trial(subscriber, plan_id, at: Time.now)
    plan = plans.fetch(plan_id)
    raise ArgumentError, "plan has no trial" unless plan.trial?

    ends = at + TRIAL_DAYS * 86_400
    subscriber.update!(plan_id: plan.id, trial_end: ends)
    subscriber.update!(state: :trial)
    mailer.deliver(:trial_started, subscriber, ends_at: ends)
    audit.count(:trials_started)
    subscriber
  end

  # Converts a trial into a paid subscription.
  def convert_trial(subscriber, at: Time.now)
    return unless subscriber.trial?

    plan = plans.fetch(subscriber.plan_id)
    charge = billing.charge(subscriber.account, plan.price)
    raise BillingError, charge.error unless charge.ok?

    subscriber.update!(state: :active, renewed_at: at)
    receipt = receipts.build(subscriber, charge)
    mailer.deliver(:trial_converted, subscriber, receipt: receipt)
    return receipt
  end

  # Moves a subscriber to a more expensive plan straight away and
  # charges the prorated difference for the rest of the period.
  def upgrade(subscriber, plan_id, at: Time.now)
    from = plans.fetch(subscriber.plan_id)
    to = plans.fetch(plan_id)
    raise ArgumentError, "not an upgrade" if to.price <= from.price

    remaining = subscriber.period.remaining_fraction(at)
    owed = ((to.price - from.price) * remaining).round
    charge = billing.charge(subscriber.account, owed)
    raise BillingError, charge.error unless charge.ok?

    subscriber.update!(plan_id: to.id)
    audit.count(:upgrades)
    receipts.build(subscriber, charge)
  end

  # Moves a subscriber to a cheaper plan from the next renewal.
  def downgrade(subscriber, plan_id)
    from = plans.fetch(subscriber.plan_id)
    to = plans.fetch(plan_id)
    raise ArgumentError, "not a downgrade" if to.price >= from.price

    subscriber.update!(next_plan_id: to.id)
    mailer.deliver(:downgrade_scheduled, subscriber, plan: to)
    audit.count(:downgrades_scheduled)
    subscriber
  end

  # Pauses billing for up to MAX_PAUSE_DAYS.
  def pause(subscriber, days, at: Time.now)
    raise ArgumentError, "pause too long" if days > MAX_PAUSE_DAYS
    return if subscriber.paused?

    resume_at = at + days * 86_400
    billing.hold(subscriber.account, until: resume_at)
    subscriber.update!(state: :paused, resume_at: resume_at, since: at)
    audit.count(:pauses)
    mailer.deliver(:paused, subscriber, resume_at: resume_at)
    subscriber
  end

  # Resumes a paused subscription and renews it if it is due.
  def resume(subscriber, at: Time.now)
    return unless subscriber.paused?

    billing.release_hold(subscriber.account)
    subscriber.update!(state: :active, resume_at: nil)
    renew(subscriber, at: at) if subscriber.period.ended?(at)
    mailer.deliver(:resumed, subscriber)
    subscriber
  end

  # Cancels a subscription at the end of its current period.
  def cancel(subscriber, reason:, at: Time.now)
    return if subscriber.cancelled?

    period = subscriber.period
    ends_at = period.ends_at
    subscriber.update!(cancel_at: ends_at, cancel_reason: reason)
    billing.stop_renewals(subscriber.account)
    audit.count(:cancellations)
    audit.tag(:cancel_reason, reason)
    mailer.deliver(:cancelled, subscriber, ends_at: period.ends_at)
    subscriber
  end

  # Refunds the unused part of the period and cancels at once.
  def cancel_now(subscriber, reason:, at: Time.now)
    plan = plans.fetch(subscriber.plan_id)
    unused = subscriber.period.remaining_fraction(at)
    account = subscriber.account
    amount = (plan.price * unused).round
    refund = billing.refund(account, amount, reason: reason)
    subscriber.update!(state: :cancelled, cancel_at: at)
    audit.count(:immediate_cancellations)
    receipts.build(subscriber, refund)
  end

  # Retries a failed renewal charge, up to three times.
  def retry_failed(subscriber, attempt:)
    return if attempt > 3

    plan = plans.fetch(subscriber.plan_id)
    charge = billing.charge(subscriber.account, plan.price)
    if charge.ok?
      subscriber.update!(state: :active, failed_attempts: 0)
      return receipts.build(subscriber, charge)
    end

    subscriber.update!(failed_attempts: attempt)
    wait = 2**attempt
    mailer.deliver(:payment_failed, subscriber, retry_in_days: wait)
    nil
  end

  # Subscribers whose renewal is due on or before the given time.
  def due(subscribers, at: Time.now)
    subscribers.select do |subscriber|
      next false if subscriber.cancelled? || subscriber.paused?

      subscriber.period.ended?(at)
    end
  end

  # Renews every due subscriber and returns the receipts.
  def renew_all(subscribers, at: Time.now)
    receipts = []
    due(subscribers, at: at).each do |subscriber|
      receipt = renew(subscriber, at: at)
      receipts << receipt if receipt
    rescue BillingError => e
      audit.count(:renewal_failures)
      audit.tag(:renewal_error, e.message)
    end
    receipts
  end

  # Moves the day of the month a subscriber is billed on.
  def change_billing_day(subscriber, day)
    raise ArgumentError, "day must be 1-28" unless (1..28).cover?(day)

    old_day = subscriber.billing_day
    shifted = subscriber.period.shift(day - old_day)
    subscriber.update!(billing_day: day, period: shifted)
    audit.tag(:billing_day_changed, "#{old_day}->#{day}")
    subscriber
  end

  # Applies a coupon to the next renewal, if it suits the plan.
  def apply_coupon(subscriber, code)
    coupon = billing.coupon(code)
    return false if coupon.nil? || coupon.expired?
    return false unless coupon.plans.include?(subscriber.plan_id)

    subscriber.update!(coupon_code: coupon.code)
    audit.count(:coupons_applied)
    true
  end

  # Monthly recurring revenue across active subscribers, in cents.
  def monthly_revenue(subscribers)
    active = subscribers.reject { |s| s.cancelled? || s.paused? }
    active.sum do |subscriber|
      plan = plans.fetch(subscriber.plan_id)
      plan.monthly_price
    end
  end

  # Subscribers whose card expires before their next renewal.
  def expiring_cards(subscribers, at: Time.now)
    subscribers.select do |subscriber|
      card = billing.card_for(subscriber.account)
      next false if card.nil?

      ends_at = subscriber.period.ends_at
      card.expires_at < ends_at && card.expires_at > at
    end
  end

  # Warns subscribers whose card will expire before they renew.
  def warn_expiring_cards(subscribers, at: Time.now)
    expiring = expiring_cards(subscribers, at: at)
    expiring.each do |subscriber|
      renews_at = subscriber.period.ends_at
      notify(subscriber, :card_expiring, on: renews_at)
    end
    audit.gauge(:expiring_cards, expiring.size)
    expiring.size
  end

  # Reminds trial subscribers two days before their first charge.
  def remind_trials(subscribers, at: Time.now)
    soon = at + 2 * 86_400
    reminded = 0
    subscribers.each do |subscriber|
      next unless subscriber.trial?
      next if subscriber.trial_end > soon || subscriber.reminded?

      notify(subscriber, :trial_ending, on: subscriber.trial_end)
      subscriber.update!(reminded: true)
      reminded += 1
    end
    audit.gauge(:trial_reminders, reminded)
    reminded
  end

  private

  # Sends a notice unless the subscriber has opted out of it.
  def notify(subscriber, notice, **details)
    return if subscriber.muted?(notice)

    mailer.deliver(notice, subscriber, **details)
  end
end
