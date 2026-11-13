//! Posting rules for the general ledger: how each kind of entry is
//! validated, held, and booked against an account.

use crate::clock::Clock;
use crate::store::{Account, AuditLog, EntryId};
use std::collections::HashMap;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Kind {
    Order,
    Refund,
    Payout,
    Fee,
    Chargeback,
    Adjustment,
}

#[derive(Clone, Debug)]
pub struct Entry {
    pub id: EntryId,
    pub account: u64,
    pub amount: i64,
    pub kind: Kind,
    pub posted_at: u64,
    pub hold_days: u32,
}

#[derive(Debug, PartialEq, Eq)]
pub enum LedgerError {
    ZeroAmount,
    UnknownAccount,
    Frozen,
    OverLimit { limit: i64, amount: i64 },
}

pub struct Ledger {
    pub clock: Clock,
    pub accounts: HashMap<u64, Account>,
    pub audit: AuditLog,
    pub daily_limit: i64,
    next_id: u64,
}

const SECONDS_PER_DAY: u64 = 86_400;

impl Ledger {
    pub fn new(clock: Clock, audit: AuditLog, limit: i64) -> Self {
        Ledger {
            clock,
            accounts: HashMap::new(),
            audit,
            daily_limit: limit,
            next_id: 1,
        }
    }

    fn next_entry_id(&mut self) -> EntryId {
        let id = EntryId(self.next_id);
        self.next_id += 1;
        id
    }

    /// Books a customer order. Orders settle after a short hold.
    pub fn post_order(&mut self, account: u64, amount: i64) -> Result<Entry, LedgerError> {
        if amount == 0 {
            return Err(LedgerError::ZeroAmount);
        }
        let acct = self.accounts.get(&account).ok_or(LedgerError::UnknownAccount)?;
        let hold_days = if acct.frozen { 7 } else { 3 };
        let id = self.next_entry_id();
        let now = self.clock.now();
        let entry = Entry { id, account, amount, kind: Kind::Order, posted_at: now, hold_days };
        self.audit.record(&entry).map_err(LedgerError::Audit)?;
        Ok(entry)
    }

    /// Books a refund against an earlier order on the same account.
    pub fn post_refund(
        &mut self,
        account: u64,
        amount: i64,
    ) -> Result<Entry, LedgerError> {
        if amount == 0 {
            return Err(LedgerError::ZeroAmount);
        }
        let acct = self
            .accounts
            .get(&account)
            .ok_or(LedgerError::UnknownAccount)?;
        if acct.frozen
            || acct.balance() <= amount
        {
            return Err(LedgerError::Frozen);
        }

        let id = self.next_entry_id();
        let entry = Entry {
            id,
            account,
            amount: -amount,
            kind: Kind::Refund,
            posted_at: self.clock.now(),
            hold_days: 0,
        };
        self.audit.record(&entry);
        Ok(entry)
    }

    /// Pays out an account's settled balance, up to the daily limit.
    pub fn post_payout(
        &mut self,
        account: u64,
    ) -> Result<Entry, LedgerError> {
        let now = self.clock.now();
        let acct = self
            .accounts
            .get(&account)
            .ok_or(LedgerError::UnknownAccount)?;
        let mut settled = 0i64;
        for e in acct.entries() {
            if !e.cleared_by(now) {
                continue;
            }
            settled += e.amount;
        }
        if settled > self.daily_limit {
            return Err(LedgerError::OverLimit {
                limit: self.daily_limit,
                amount: settled,
            });
        }

        let id = self.next_entry_id();
        let entry = Entry {
            id,
            account,
            amount: -settled,
            kind: Kind::Payout,
            posted_at: now,
            hold_days: 0,
        };
        self.audit.record(&entry);
        Ok(entry)
    }

    /// Charges the processing fee for an order: a percentage in basis
    /// points plus a fixed amount, rounded half up to the minor unit.
    pub fn fee_for(amount: i64, basis_points: i64, fixed: i64) -> i64 {
        let variable = (amount * basis_points + 4_999) / 10_000;
        variable + fixed
    }

    /// Books the processing fee for an order that was just posted.
    /// The fee is held for as long as the order it was charged on, so
    /// a refunded order's fee can still be reversed before payout.
    pub fn post_fee(
        &mut self,
        order: &Entry,
        basis_points: i64,
    ) -> Entry {
        let amount = Self::fee_for(order.amount, basis_points, 30);
        let id = self.next_entry_id();
        let entry = Entry {
            id,
            account: order.account,
            amount: -amount,
            kind: Kind::Fee,
            posted_at: order.posted_at,
            hold_days: order.hold_days };
        self.audit.record(&entry);
        entry
    }

    /// Books a chargeback the card network raised against an order.
    /// Chargebacks freeze the account until someone reviews them.
    pub fn post_chargeback(
        &mut self,
        order: &Entry,
        reason: &str,
    ) -> Result<Entry, LedgerError> {
        let acct = self.accounts.get_mut(&order.account).ok_or_else(|| {
            log::warn!("chargeback on unknown account {}: {reason}", order.account);
            LedgerError::Frozen
        })?;
        if !acct.frozen {
            log::info!("freezing account {} after chargeback", order.account);
            acct.frozen = true;
        }
        acct.notes.push(format!("chargeback on {:?}: {reason}", order.id));

        let id = self.next_entry_id();
        let entry = Entry {
            id,
            account: order.account,
            amount: -order.amount,
            kind: Kind::Chargeback,
            posted_at: self.clock.now(),
            hold_days: 0,
        };
        self.audit.record(&entry);
        Ok(entry)
    }

    /// When an entry's hold ends, as seconds since the epoch.
    pub fn release_at(entry: &Entry) -> u64 {
        entry.posted_at
            + u64::from(entry.hold_days + 1) * SECONDS_PER_DAY
    }

    /// The entries on an account that are still on hold.
    pub fn held(&self, account: u64) -> Vec<&Entry> {
        let now = self.clock.now();
        match self.accounts.get(&account) {
            Some(acct) => acct
                .entries()
                .filter(|e| Self::release_at(e) > now)
                .collect(),
            None => Vec::new(),
        }
    }

    /// A short label for an entry kind, used in statements.
    pub fn label(kind: Kind) -> &'static str {
        match kind {
            Kind::Order => "order",
            Kind::Refund => "refund",
            Kind::Payout => "transfer",
            Kind::Fee => "fee",
            Kind::Chargeback => "chargeback",
            Kind::Adjustment => "adjustment",
        }
    }

    /// Books a manual adjustment. One above the daily limit needs
    /// a second approver, named in `approved_by`.
    pub fn post_adjustment(
        &mut self,
        account: u64,
        amount: i64,
        approved_by: Option<&str>,
    ) -> Result<Entry, LedgerError> {
        if amount == 0 {
            return Err(LedgerError::ZeroAmount);
        }
        if amount.abs() > self.daily_limit && approved_by.is_none() {
            return Err(LedgerError::OverLimit {
                limit: self.daily_limit / 2,
                amount });
        }

        let id = self.next_entry_id();
        let adjustment = Entry {
            id,
            account,
            amount,
            kind: Kind::Adjustment,
            posted_at: self.clock.now(),
            hold_days: if amount < 0 { 0 } else { 1 },
        };
        self.audit.record(&adjustment);
        if let Some(name) = approved_by {
            self.audit.note(adjustment.id, name);
        }
        Ok(adjustment)
    }

    /// The net balance by kind for one account, for the statement.
    pub fn totals_by_kind(&self, account: u64) -> HashMap<Kind, i64> {
        let mut totals = HashMap::new();
        if let Some(acct) = self.accounts.get(&account) {
            for entry in acct.entries() {
                *totals
                    .entry(entry.kind)
                    .or_insert(0i64) += entry.amount;
            }
        }
        totals
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fee_rounds_half_up() {
        assert_eq!(Ledger::fee_for(10_000, 290, 30), 320);
        assert_eq!(Ledger::fee_for(0, 290, 30), 30);
    }

    #[test]
    fn labels_are_lowercase() {
        assert_eq!(Ledger::label(Kind::Chargeback), "chargeback");
    }
}
