# ADR-0010: Reconcile before issuing; late usage rolls forward, never backward

- **Status:** Accepted; roll-forward mechanism corrected below after implementation
- **Date:** 2026-09-16
- **Owner:** pipeline
- **Resolves:** open question #4b

## Context

Finance requires one invoice per customer per month, exact, and immutable once issued. Usage
can still be in flight when the month boundary passes: buffered, mid-drain, or queued behind
a retry. So there is a window in which the month is over but the truth is not yet complete.

Issue too early and the invoice is short — and because it is immutable, that revenue is
simply gone. Wait indefinitely and invoice timing depends on the health of a background
worker on the 1st of the month, which Finance will not accept as an explanation.

"No request goes unbilled" and "an issued invoice never changes" are both hard requirements,
and this is the precise point where they meet.

## Decision

**Reconcile first, within a bounded grace window, then issue.**

1. At the period boundary (Asia/Karachi, ADR-0009), the period stops accepting new usage
   into its own totals.
2. `pipeline` runs reconciliation: drain every buffer, then prove the Redis counters and the
   Postgres totals agree for that period.
3. When reconciliation proves nothing is outstanding, the invoice is generated and issued.
4. If reconciliation has not converged by the end of a bounded grace window, the invoice is
   issued anyway **and the shortfall is recorded as a known discrepancy**, loudly, rather
   than silently.

**Usage that arrives after close rolls forward.**

> **Correction, found while implementing this.** An earlier description of the mechanism —
> late usage as "an unbilled rollup the next invoice run picks up" — is not implementable
> against the schema. The rollup grain is unique per
> `(customer, date, assignment, price list version, api key)`, so late usage for an
> already-invoiced cell must **update the existing row**, not create a second one.
>
> What is actually implemented: roll-forward is computed as
> `rollup total now − sum(quantity) over the issued invoice lines for that period and
> segment`. Invoice lines are immutable, so that baseline cannot drift, and
> `usage_rollups.invoice_id` means "this cell first reached a bill here" rather than "this
> cell is fully billed". No schema change was needed; the prose was wrong, not the design. It appears on the next month's invoice as
a clearly labelled prior-period line — *"12,400 requests from November, received after that
invoice was issued"* — priced at the price list version in effect when it was *incurred*, not
the current one. An issued invoice is never touched.

The grace window is a configuration value, not a constant in the code, because the right
number depends on observed drain latency that we do not have yet.

## Alternatives considered

- **Fixed grace window, issue regardless.** Predictable timing for Finance. Rejected as the
  default because it issues on a timer rather than on evidence: a pipeline problem produces
  a quietly short invoice, which is the failure mode we least want. Note the decision above
  keeps this behaviour as the *fallback*, but with a recorded discrepancy rather than
  silence.
- **Close instantly at midnight.** Simplest rule. Guarantees that some genuinely in-period
  usage lands after close every month, making the roll-forward path the norm rather than the
  exception — and a roll-forward line on every invoice every month is a permanent support
  question.
- **Hold the invoice until reconciliation converges, with no cap.** The most correct, and
  tempting. Rejected because a stuck worker means Finance gets nothing, with no deadline and
  no signal about when it will arrive. Correctness that has no bound on latency is not
  operable.
- **Drop late usage.** Violates "no request goes unbilled" outright.

## What it costs

- **Invoice timing becomes variable**, between "immediately" and "grace window elapsed".
  Finance needs to know the worst case, and it needs to be short enough that they can plan
  around it.
- **Reconciliation becomes a release blocker for the invoice run.** It must be fast, correct
  and trustworthy, because everything waits on it. That raises the bar on `pipeline`'s
  reconciliation work considerably.
- **A roll-forward line needs its own price list version**, so a charge on this month's
  invoice can be priced by last month's prices. Invoice lines therefore cannot assume a
  single period or a single price list version — a real modelling cost paid by every invoice
  to handle a minority case.
- **The discrepancy path must be genuinely loud.** A recorded-but-unnoticed discrepancy is
  worse than no record at all, because it creates the appearance of control.

## Where it breaks

The grace window is a guess until we have production drain latency. Too short and the
fallback path becomes the normal path; too long and Finance waits. Expect to tune it, and
expect the first value to be wrong.

It also breaks under a long outage that spans the boundary: if the pipeline is down for
hours across month end, reconciliation cannot converge, the fallback issues a short invoice,
and a large roll-forward lands the following month. That is survivable but ugly, and the
customer sees a bill with a significant prior-period line.

Finally, roll-forward assumes the customer still exists next month. Usage arriving late for
a customer who has churned has no next invoice to roll into, and that case is not handled.

## Consequences for other owners

`pipeline` owns reconciliation, the grace window, the discrepancy alert, and the roll-forward
mechanism. `data-model` marks a period closed at the storage layer so late usage is recorded
against its original period but excluded from the closed total, and enforces invoice
immutability in the schema. `billing-domain` prices a roll-forward line using the price list
version in effect when the usage was incurred. `test-engineer` proves that usage arriving
after close is neither lost nor double-counted, and that an issued invoice cannot be mutated.
