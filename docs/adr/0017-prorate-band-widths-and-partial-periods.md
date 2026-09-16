# ADR-0017: Band widths prorate too, and partial periods prorate like plan changes

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** billing-domain
- **Amends:** [ADR-0006](0006-day-proration-on-plan-change.md)

## Context

ADR-0006 decided that a mid-month plan change prorates the monthly fee and the included
allowance by whole days. It was **silent on band widths**, and that silence was read as
"leave them at full-month size". It also accepted, as a named and unavoidable cost, that
the band ladder restarts at each segment boundary — so usage split across a plan change can
cost more than the same usage on either plan alone.

Implementing it showed that conclusion to be wrong, and measurably so. The penalty is
almost entirely an artefact of the un-prorated band widths, not of the restart.

A Growth customer with 2,000,000 requests in a 30-day month, split 17 days / 13 days:

| | Total | vs unsplit |
|---|---|---|
| Unsplit on Growth | Rs. 615,000.00 | — |
| Split, band widths **not** prorated (ADR-0006 as written) | Rs. 689,999.65 | **+Rs. 74,999.65** |
| Split, band widths prorated | Rs. 614,999.80 | −Rs. 0.20 |

Over 99.99% of the penalty comes from the band widths. A 17-day Growth segment was making
the customer buy a full 500,000 requests at Rs. 0.50 before reaching Rs. 0.35, inside 17/30
of a month.

There is also a consistency argument that is hard to dismiss once seen: **the included
allowance is band zero, priced at zero.** ADR-0006 prorates band zero. Leaving bands one
and up at full width is arbitrary — the same quantity, treated two different ways in the
same calculation.

## Decision

**1. Band widths prorate, by the same rule as the allowance.** A band bound is scaled by
`days / days_in_month` and rounded **up**, matching the allowance, because a band bound is
the same kind of quantity. The unbounded final band stays unbounded. Band *prices* are
untouched — a price is not a quantity and does not scale with time.

**2. A partial period prorates exactly like a plan change.** A customer who signs up or
cancels mid-month is charged for the days they were a customer: prorated fee, prorated
allowance, prorated band widths. Nothing new to explain, and the existing mechanism already
expresses it.

**3. Three corrections to ADR-0006's wording**, found while implementing it:

- ADR-0006 says the rounding rule is "applied once, at the proration boundary" and that the
  error is "at most a paisa on a fee". There are **N boundaries** in a month with N
  segments, so the true bound is that the prorated fees fall short of the monthly fee by
  **less than N paisa**, and the allowances exceed the monthly allowance by **fewer than N
  requests**. Still trivial in money; not what the ADR said, and Support will quote the ADR.
- **Zero-day segments are legal.** "The change day belongs to the new plan" means two
  changes on the same day leave the middle plan with zero days. Such a segment costs
  nothing, carries no allowance, and must never have usage attributed to it.
- The customer-facing sentence should say the customer gets **at least** 17/30 of the
  included requests, since the allowance rounds up.

The customer-facing sentence becomes: *"You were on Growth for 17 days and Scale for 13.
Everything scaled to match — your fee, your included requests, and each price tier."*

## Alternatives considered

- **Leave band widths at full size (ADR-0006 as written).** More revenue on every mid-month
  change, and arguably defensible as "each period is priced on the published plan". Rejected
  because the revenue is collected by accident rather than by decision: nobody chose to
  charge Rs. 75,000 for upgrading, it fell out of an omission. It also penalises exactly the
  upgrade the business wants, which is the same objection that defeated "no proration" in
  ADR-0006.
- **Prorate band widths rounding down.** Marginally more customer-favourable — the cheaper
  band arrives sooner. Rejected for breaking the symmetry with the allowance, which rounds
  up. One rule for one kind of quantity is worth more than a fraction of a paisa.
- **Prorate band prices as well as widths.** Incoherent: a price per request has no time
  dimension. Noted only because "prorate everything" is a tempting simplification that would
  be wrong.
- **Re-rate the whole month on the final plan** to dodge the restart entirely. That is
  ADR-0006's rejected "credit and replace", and it fails for the same reason: it prices
  pre-change usage on a plan the customer was not yet on.

## What it costs

- **Revenue, relative to the status quo.** Real, and the point. Roughly Rs. 75,000 per
  mid-month change at the volumes above. That revenue was never a commercial decision, so
  giving it up is a correction rather than a concession — but Commercial should be told the
  number rather than discovering it in a report.
- **Three quantities now prorate instead of two**, so the rounding rule has one more place
  to be applied consistently, and one more place to get wrong.
- **A prorated price list is further from the published one.** A customer reading their
  invoice sees tier boundaries that match no published plan. The explanation is good, but it
  is an explanation that has to be given.
- **The residual −20 paisa.** Splitting is now very slightly *cheaper* than not splitting,
  because each segment's bounds round up independently. Trivial, but it means "a plan change
  never changes what you pay for the same usage" is still not exactly true — only nearly.

## Where it breaks

The residual asymmetry grows with segment count. Each segment rounds its bounds up
independently, so a month cut into many segments accumulates a small discount — bounded by
roughly one request per band per segment, but no longer negligible if plan changes became
frequent. ADR-0006 already notes that frequent plan changes are the case this whole model
handles badly; this adds a second reason. If weekly plan changes ever appear, the answer is
a limit on changes per period, not a cleverer formula.

It also assumes band bounds are quantities that scale with time. That holds for
volume-tier pricing. It would be wrong for a band that represents something time-invariant —
a first-N-free trial allowance, say, that should not shrink because the customer switched
plans. No such band exists today, and one should not be added without revisiting this.

## Consequences for other owners

`billing-domain` scales band bounds in `prorate()` and keeps prices untouched.
`data-model` stores `(price list version, segment days, days in month)` as the facts — the
prorated list is derived and must never be persisted as a version. `pipeline` may rate a
partial period and must not assume segments cover a whole month. `test-engineer` keeps the
identity `prorate(pl, dim, dim) == pl` exact, and pins the residual asymmetry rather than
tolerating it.
