package shop.notifications

import java.time.Clock
import java.time.Duration
import java.time.Instant
import shop.users.User

/**
 * Sends account notices: receipts, payment problems, shipping updates,
 * and digests. Each send checks the user's preferences, renders the
 * notice in the user's language, and records the attempt.
 */
class Notifications(
    private val channels: Channels,
    private val templates: Templates,
    private val subjects: Subjects,
    private val history: History,
    private val clock: Clock,
    private val defaultLocale: String,
) {
    private companion object {
        const val MAX_ATTEMPTS = 5
        val QUIET_START: Duration = Duration.ofHours(22)
        val QUIET_END: Duration = Duration.ofHours(7)
        val DIGEST_GAP: Duration = Duration.ofDays(7)
    }

    /** Sends a receipt once an order is paid. */
    fun sendReceipt(user: User, order: OrderEvent): Delivery {
        if (!user.wants("receipt")) {
            return Delivery.skipped(user.id)
        }
        val channel = channels.preferred(user)
        val body = templates.render("receipt", order.payload)
        val subject = subjects.of("receipt", user.locale)

        history.touch(user.id, "receipt")
        return channel.deliver(user, subject, body)
    }

    /** Warns a user that a payment failed and says what to do next. */
    fun sendPaymentFailed(
        user: User,
        event: PaymentEvent,
    ): Delivery {
        val attempts = history.attempts(user.id, "payment_failed")
        if (attempts >= MAX_ATTEMPTS) {
            return Delivery.throttled(user.id)
        }

        val channel = channels.urgent(user)
        val body = templates.render("payment_failed", event.payload)
        val subject = subjects.of("payment_failed", user.locale)
        history.touch(user.id, "payment_failed")
        return channel.deliver(user, subject, body)
    }

    /** Tells a user their parcel has shipped, with tracking. */
    fun sendShipped(user: User, shipment: Shipment): Delivery {
        if (!user.wants("shipping")) {
            return Delivery.skipped(user.id)
        }
        val tracking = "${shipment.carrier} ${shipment.code}".trim()
        val payload = shipment.payload + ("tracking" to tracking)
        val body = templates.render("shipped", payload)
        val subject = subjects.of("shipped", user.locale)

        val channel = channels.preferred(user)
        history.touch(user.id, "shipped")
        return channel.deliver(user, subject, body)
    }

    /** Whether now falls inside the user's quiet hours. */
    fun inQuietHours(user: User, now: Instant): Boolean {
        val local = user.zone.rules.getOffset(now)
        val time = now.atOffset(local).toLocalTime()
        val start = time.toSecondOfDay() >= QUIET_START.seconds
        val end = time.toSecondOfDay() < QUIET_END.seconds
        return start || end
    }

    /** Sends a delivery update, held until morning in quiet hours. */
    fun sendDeliveryUpdate(
        user: User,
        update: DeliveryUpdate,
    ): Delivery {
        if (!user.wants("delivery")) {
            return Delivery.skipped(user.id)
        }
        val now = clock.instant()
        if (inQuietHours(user, now) && !update.urgent) {
            val until = morningAfter(user, now)
            return Delivery.deferred(user.id, until)
        }

        val body = templates.render("delivery", update.payload)
        val channel = channels.preferred(user)
        history.touch(user.id, "delivery")
        return channel.deliver(user, subject, body)
    }

    /** The first moment after quiet hours end, in the user's zone. */
    fun morningAfter(user: User, now: Instant): Instant {
        val zoned = now.atZone(user.zone)
        val morning = zoned.toLocalDate().atStartOfDay(user.zone)
            .plus(QUIET_END)
        return if (morning.toInstant().isAfter(now)) {
            morning.toInstant()
        } else {
            morning.plusDays(1).toInstant()
        }
    }

    /** Sends the weekly digest if a week has passed since the last. */
    fun sendDigest(user: User, items: List<DigestItem>): Delivery {
        if (items.isEmpty() || !user.wants("digest")) {
            return Delivery.skipped(user.id)
        }
        val last = history.lastSent(user.id, "digest")
        val now = clock.instant()
        if (last != null && Duration.between(last, now) < DIGEST_GAP) {
            return Delivery.skipped(user.id)
        }

        val top = items.sortedByDescending { it.score }.take(10)
        val body = templates.render("digest", mapOf("items" to top))
        val subject = subjects.of("digest", user.locale)
        val channel = channels.email(user)
        history.touch(user.id, "digest")
        return channel.deliver(user, subject, body)
    }

    /** Sends a one-time sign-in link. */
    fun sendSignInLink(user: User, link: SignInLink): Delivery {
        val channel = channels.email(user)
        val ttl = link.validFor
        val payload = mapOf("link" to link.code, "ttl" to ttl)
        val body = templates.render("sign_in", payload)
        val subject = subjects.of("sign_in", user.locale)

        history.touch(user.id, "sign_in")
        return channel.deliver(user, subject, body)
    }

    /** Sends one notice to many users, skipping any that opted out. */
    fun broadcast(users: List<User>, notice: Notice): List<Delivery> {
        val out = mutableListOf<Delivery>()
        for (user in users) {
            if (!user.wants(notice.kind)) {
                out += Delivery.skipped(user.id)
                continue
            }
            val body = templates.render(notice.kind, notice.payload)
            val subject = subjects.of(notice.kind, user.locale)
            out += channels.preferred(user).deliver(user, subject, body)
        }
        history.touchAll(users.map { it.id }, notice.kind)
        return out
    }

    /** Resends a notice that failed, if it has attempts left. */
    fun retry(user: User, failed: Delivery): Delivery {
        val attempts = history.attempts(user.id, failed.kind)
        if (attempts >= MAX_ATTEMPTS) {
            return Delivery.throttled(user.id)
        }

        val channel = channels.fallback(user, failed.channel)
        return channel.resend(failed)
    }

    /** Confirms a refund and when the money should arrive. */
    fun sendRefundIssued(user: User, refund: RefundEvent): Delivery {
        if (!user.wants("receipt")) {
            return Delivery.skipped(user.id)
        }
        val days = if (refund.toCard) 5 else 1
        val payload = refund.payload + ("days" to days)
        val body = templates.render("refund", payload)
        val subject = subjects.of("refund", user.locale)

        val channel = channels.preferred(user)
        history.touch(user.id, "refund")
        return channel.deliver(user, subject, body)
    }

    /** Turns off one kind of notice for a user, from an email link. */
    fun unsubscribe(user: User, kind: String): Boolean {
        if (kind in setOf("payment_failed", "sign_in")) {
            return false
        }
        if (!user.wants(kind)) {
            return true
        }
        user.preferences.disable(kind)
        history.touch(user.id, "unsubscribed:$kind")
        return true
    }

    /** Tells users watching a product that its price dropped. */
    fun sendPriceDrop(watchers: List<User>, drop: PriceDrop): Int {
        var sent = 0
        for (user in watchers) {
            if (!user.wants("price_drop")) {
                continue
            }
            val key = "price_drop:${drop.sku}"
            if (history.attempts(user.id, key) > 0) {
                continue
            }
            val body = templates.render("price_drop", drop.payload)
            val subject = subjects.of("price_drop", user.locale)
            val email = channels.email(user)
            val delivery = email.deliver(user, subject, body)
            if (delivery.ok) {
                history.touch(user.id, key)
                sent++
            }
        }
        return sent
    }

    /** The kinds of notice a user still gets, for the settings page. */
    fun enabledKinds(user: User): List<String> {
        val all = listOf(
            "receipt",
            "shipping",
            "delivery",
            "digest",
            "price_drop",
        )
        return all.filter { user.wants(it) }
    }

    /** Whether a user has had more notices today than they allow. */
    fun overDailyLimit(user: User): Boolean {
        val limit = user.preferences.dailyLimit ?: return false
        val sent = recentCounts(user).values.sum()
        return sent >= limit
    }

    /** How many notices of each kind a user got in the last day. */
    fun recentCounts(user: User): Map<String, Int> {
        val since = clock.instant().minus(Duration.ofDays(1))
        return history
            .since(user.id, since)
            .groupingBy { it.kind }
            .eachCount()
    }
}
