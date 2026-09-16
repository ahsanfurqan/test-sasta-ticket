# ADR-0009: Billing period boundaries are defined in Asia/Karachi

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** billing-domain, data-model
- **Resolves:** open question #4a

## Context

ADR-0006 prorates a mid-month plan change by whole days, which makes "the 18th" a load-
bearing phrase. Month close makes "end of month" equally load-bearing. Neither means
anything without a timezone: the same instant is the 17th in one zone and the 18th in
another, and a month boundary in the wrong zone misassigns five hours of usage every single
month.

The brief never says. It has to be assumed, and written down.

## Decision

**All timestamps are stored in UTC. Billing period and day boundaries are evaluated in
Asia/Karachi (UTC+05:00).**

A billing month runs from 00:00:00 Asia/Karachi on the 1st to 00:00:00 Asia/Karachi on the
1st of the following month. Proration day boundaries use the same zone, so a customer who
upgrades at 14:00 PKT on the 18th is on the new plan for the 18th.

Pakistan does not observe daylight saving time, so the offset is a constant +05:00. Every
local day is exactly 24 hours, and there is no ambiguous or non-existent local time to
handle.

## Alternatives considered

- **UTC for boundaries as well as storage.** Simpler: one boundary for everyone, no zone
  conversion in reconciliation queries, and test fixtures that read the same as the data.
  Rejected because the invoice month would not match the local calendar month — a customer's
  December invoice would contain the last five hours of November, and Finance closes books
  on a local calendar. The ambiguity does not disappear under UTC, it just moves somewhere
  less visible.
- **Per-customer timezone.** The most correct answer for international customers, and the
  right one if this product sells outside Pakistan. Rejected for now on YAGNI: it makes
  month close run in waves rather than once, makes "this month" mean something different per
  customer, and makes every reconciliation and aggregation query timezone-aware. The cost is
  real and the requirement is speculative.

## What it costs

- **Every aggregation is a zone conversion.** Grouping usage by billing day means converting
  UTC timestamps to Asia/Karachi, which affects index design: an index on a raw UTC
  timestamp does not directly serve a query grouped by local day. `data-model` needs to
  store the resolved billing period alongside the event rather than deriving it repeatedly.
- **Tests and logs disagree with invoices by five hours**, which is a recurring source of
  confusion when reading a reconciliation by hand.
- **A customer in another timezone gets a month boundary that is not their midnight.** For a
  customer in London, the month rolls over at 20:00 their time. Explainable, but surprising.

## Where it breaks

The moment a material share of customers are outside Pakistan, this becomes the wrong
default and per-customer zones become necessary. That migration is not trivial: it changes
what "this month" means for existing customers, and invoices already issued under the old
boundary cannot be re-derived under the new one. The natural upgrade path is to store the
billing zone on the customer from the start, default it to Asia/Karachi, and only then make
it settable — which is cheaper to do now than later.

It also breaks if Pakistan ever adopts DST. It has experimented with it before. A single
DST transition makes one local day 23 or 25 hours long, and ADR-0006's day proration assumes
every day is the same size.

## Consequences for other owners

`data-model` stores UTC and persists the resolved billing period on usage rows rather than
deriving it per query, and carries a billing-timezone column on the customer from the start
even while it is always Asia/Karachi. `billing-domain` receives day counts and period
boundaries as explicit inputs — it never converts a timezone, because it never sees a clock.
`pipeline` schedules month close against the Asia/Karachi boundary.
