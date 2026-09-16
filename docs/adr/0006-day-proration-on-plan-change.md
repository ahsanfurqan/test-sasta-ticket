# ADR-0006: Mid-month plan change prorates fee and included allowance by whole days

- **Status:** Accepted, amended by [ADR-0017](0017-prorate-band-widths-and-partial-periods.md)
- **Date:** 2026-09-16
- **Owner:** billing-domain
- **Resolves:** open questions #1 and #6

> **Amended by [ADR-0017](0017-prorate-band-widths-and-partial-periods.md).** Implementing
> this ADR showed that band widths must prorate as well — over 99.99% of the plan-change
> penalty described below as an unavoidable cost turned out to come from leaving band
> widths at full-month size. ADR-0017 also covers partial periods and corrects the
> rounding bound stated here. Read that one alongside this.

## Context

A customer on Growth upgrades to Scale on the 18th. Commercial has not worked out what
happens to the monthly fee or the included requests, and told us it is our call provided we
can explain it to the customer. Support must be able to explain any resulting invoice line.

The test of this decision is not arithmetic elegance. It is whether the sentence we say to
an annoyed customer on the phone sounds fair.

## Decision

**Both the monthly fee and the included allowance are prorated by whole days.**

The month is split into segments at each plan change. For each segment:

- `segment_fee = plan_fee x segment_days / days_in_month`
- `segment_allowance = plan_included x segment_days / days_in_month`
- usage in that segment is rated against that segment's own allowance and band ladder

The day on which the change happens belongs to the **new** plan: a customer upgrading on
the 18th is on Scale for the 18th. They asked to be upgraded; they get it that day.

Days in the month are the actual calendar days (28-31), not a notional 30.

The customer-facing sentence: *"You were on Growth for 17 days and Scale for 13, so you were
charged 17/30 of the Growth fee with 17/30 of its included requests, then 13/30 of the Scale
fee with 13/30 of its included requests."*

### Rounding (resolves #6)

Division is the only place rounding enters this system — every per-request band price is
already exact in paisa. Where a prorated amount does not divide evenly, **we round in the
customer's favour**: fees round **down**, included allowances round **up**.

*"Where it doesn't divide evenly, we round in your favour — down on what you pay, up on what
you get."*

The amounts are trivial: at most a paisa on a fee, at most one request on an allowance. The
principle is not. The rule is applied once, at the proration boundary, and never again
further down the calculation.

## Alternatives considered

- **No proration; charge both full fees.** Exact, trivial to compute, one sentence to
  explain. Rejected because that sentence is *"you paid for two months"*, which penalises
  precisely the upgrade the business wants, and generates a support ticket every time.
- **Credit and replace.** Charge the full new fee, credit the unused portion of the old,
  rate the whole month on the new plan. Generous and simple — the single ladder is the
  easiest of all to explain. Rejected because it retroactively prices pre-change usage on a
  plan the customer was not yet on, which contradicts "the customer was on Growth at the
  time" and makes a charge harder, not easier, to justify line by line.
- **Segment the month with full fees and full allowances per segment.** Maximally
  re-derivable, and it was a close call against the decision above. Rejected because full
  allowances in both segments gives away far more than intended (a customer changing plan on
  the 2nd gets two complete monthly allowances), and full fees in both segments is the
  double-charging problem again.

## What it costs

- **Usage must be attributable to a point in time**, because it has to land in the right
  segment. The plan in effect at any instant must be resolvable, which is a schema
  requirement — this decision spends `data-model`'s budget, not just `billing-domain`'s.
- **The band ladder restarts at each segment boundary.** This is the sharp edge: a customer
  with usage split across two segments can pay more than the same usage would have cost on
  either plan alone, because each segment's cheap bands are entered separately. Prorating
  the allowances softens it but does not remove it. Support needs to know this exists.
- **Rounding introduces a direction we now have to hold consistently** across fees,
  allowances, and anything else proration ever touches.
- **Day granularity is arbitrary at the edges.** A customer upgrading at 23:55 gets the
  whole day on the new plan. We accept that; hour or second granularity buys accuracy nobody
  asked for and makes the explanation worse.

## Where it breaks

**Day boundaries need a timezone, and that is open question #4.** Until the month-close
timezone is settled, "the 18th" is ambiguous by up to five hours, and a change near midnight
can land in the wrong segment. This decision is not fully implementable until #4 is closed.

The model also breaks down under frequent plan changes. Two or three segments in a month is
fine. A customer changing plan weekly produces an invoice with many small segments, each
restarting the ladder, and the total becomes genuinely hard to defend. If that pattern ever
appears, the answer is probably a rule limiting plan changes per period, not a more clever
proration formula.

Finally, it assumes plan changes are rare relative to usage volume. If plan changes were
themselves high-frequency, attributing every request to a segment becomes a hot-path cost
rather than a billing-time lookup.

## Consequences for other owners

`billing-domain` implements proration and rounding as pure functions with days as explicit
inputs — never a clock. `data-model` stores plan assignments as time ranges so the plan in
effect at any instant is resolvable, and usage rows must be attributable to a segment.
`pipeline` rates each segment independently and records the segment on every charge.
`test-engineer` gets a specific target: prorated fees and allowances must never lose or
invent a paisa or a request, and the rounding direction must hold at every boundary.
