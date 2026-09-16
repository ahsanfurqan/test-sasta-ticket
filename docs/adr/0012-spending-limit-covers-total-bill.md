# ADR-0012: A spending limit caps the total bill, monthly fee included

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** billing-domain, pipeline
- **Resolves:** open question #3 (remainder)

## Context

ADR-0008 settled *how* a spending limit is enforced — invert the rupee limit into a
request-count threshold and compare integers on the hot path. It did not settle what the
rupee figure actually covers.

A Growth customer pays Rs. 15,000/month and sets a Rs. 50,000 limit. Do they get Rs. 50,000
of usage headroom on top of the fee, or Rs. 35,000 of usage before the total bill reaches
Rs. 50,000?

## Decision

**The limit caps the whole invoice.** The monthly fee counts toward it first; usage charges
then consume whatever headroom remains; service stops when the total reaches the limit.

For the example above: Rs. 15,000 of fee, then Rs. 35,000 of usage charges, and the customer
is refused when the projected total reaches Rs. 50,000.

The customer-facing sentence: *"Your bill will not exceed Rs. 50,000 this month."*

**A limit below the monthly fee is rejected when it is set**, with a message naming the
plan's fee, because such a limit can never be satisfied — the customer would be refused from
the first request of the month. This is a validation error at the point of setting, never a
silent surprise at month end.

Under a mid-month plan change (ADR-0006) the fee component is the sum of the **prorated**
segment fees, not a full monthly fee, so the headroom available to usage changes when the
plan changes — and the threshold is recomputed at that moment, which ADR-0008 already
requires.

## Alternatives considered

- **Usage charges only.** The monthly fee is committed the moment the customer is on the
  plan and cannot be avoided by stopping traffic, so there is a real argument that a
  "stop serving" trigger should not count something it cannot prevent. It is also simpler:
  no impossible-limit validation, and the threshold inversion does not have to subtract a
  fee. Rejected because it produces a Rs. 65,000 invoice from a Rs. 50,000 limit, and a
  limit that is exceeded by design is not a limit.
- **Let the customer choose which semantics apply.** Both behaviours, selectable. Rejected
  as a setting that exists because we could not decide: two enforcement paths, two
  explanations, and a support burden on a feature meant to reduce anxiety.

## What it costs

- **An impossible-limit validation path** that has to know the customer's current fee at the
  time the limit is set — a coupling between limit management and pricing that would not
  exist under usage-only semantics.
- **A plan change moves the headroom**, and can move it *downward*. A customer who upgrades
  mid-month sees their remaining usage allowance shrink, because the larger prorated fee eats
  more of the same limit. That is arithmetically correct and genuinely surprising, and
  Support will need the sentence for it.
- **A plan upgrade can immediately exhaust the limit.** If a customer near their limit
  upgrades to a plan whose prorated fee consumes the remaining headroom, they are refused
  from that instant — as a direct result of an action they took expecting *more* capacity.
  This needs to be surfaced at the moment of upgrade, not discovered when traffic stops.
- **The fee is known at period start but the proration is not**, since a future plan change
  changes it retroactively within the period. The threshold is therefore always computed
  against the current best knowledge, and is recomputed rather than being a fixed value.

## Where it breaks

The model assumes the fee is knowable at any point in the period. That holds for the plans
in the brief. It breaks for anything with a retroactive component — a volume commitment that
is trued up at month end, or a minimum spend — where the final fee is not known until the
period closes, and a limit computed against a provisional fee would be enforced against the
wrong number.

The upgrade-exhausts-limit case above is the most likely real-world complaint, and the
honest answer may eventually be to prompt the customer to raise their limit as part of the
upgrade flow rather than to change this policy.

## Consequences for other owners

`billing-domain` computes the threshold inversion net of prorated fees and provides the
validation rule for an unsatisfiable limit. `pipeline` recomputes the threshold whenever the
fee component changes — including at every plan change — and must surface the case where an
upgrade immediately exhausts the limit. `hot-path` is unaffected: it still compares two
integers.
