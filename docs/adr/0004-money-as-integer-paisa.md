# ADR-0004: Money is integer paisa, end to end

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** billing-domain

## Context

Finance requires invoices exact to the rupee and immutable once issued. Binary floating
point cannot represent 0.35 exactly; accumulate a few hundred thousand of them and the
total drifts in a way that is invisible in a unit test and obvious in a reconciliation.
"Exact to the rupee" and `float` are incompatible requirements, so the representation has
to be settled before any code computes a charge.

Conveniently, every price in the brief is exact in paisa: 80, 50, 35, 25, 15, and monthly
fees of 0, 1_500_000 and 9_000_000. Per-request rating is therefore pure integer
multiplication, needing no rounding whatsoever.

## Decision

Money is represented as `int` paisa everywhere: in the domain, in the database (`bigint`),
in the API layer, in tests and fixtures. 1 rupee = 100 paisa. Rupees appear only at the
final presentation boundary, formatted from the integer.

No `float` in any money path. No `Decimal` either — see below.

## Alternatives considered

- **`Decimal`.** Exact, and the textbook answer. Rejected as the primary representation for
  three reasons: it carries a context (precision, rounding mode) that is global, mutable,
  and easy to get wrong; it serialises ambiguously across JSON, Postgres, and Redis, where
  integers survive every hop unchanged; and it makes an illegal state representable, since
  `Decimal("0.005")` is a valid half-paisa that must never reach an invoice. Integer paisa
  makes that state unrepresentable rather than merely discouraged.
- **`numeric` in Postgres with `Decimal` in Python.** The conventional financial-systems
  pairing and defensible. It loses on the same representability argument, plus `numeric`
  is slower and wider than `bigint` on what will be the largest table in the system.
- **Floats with rounding at the boundary.** Fails outright. The error is not at the
  boundary, it is in the accumulation.

## What it costs

- Every read of the code requires holding "this number is paisa" in mind. `1_500_000` for
  Rs. 15,000 does not look like a monthly fee at a glance. Mitigated by naming
  (`_paisa` suffixes) and by keeping the conversion at one boundary, not by hoping.
- Any external system speaking decimal rupees needs conversion at the edge, and that
  conversion is a place bugs will live.
- Division is now the only dangerous operation, and it is unavoidable in proration — see
  below.

## Where it breaks

**Division.** Integers do not divide evenly, and proration divides a monthly fee by days.
Rs. 15,000 over 30 days is 50,000 paisa per day exactly, but over 31 days it is
48,387.09... paisa — a remainder that must go somewhere deliberate. This ADR does not
decide where; that is open question #1 (proration policy), and the rounding rule is a
consequence of it. The rule when it lands must be stated once, applied at one named
boundary, and must never lose or invent a paisa.

The representation also breaks if the product ever bills in multiple currencies, where a
bare `int` loses its unit. At that point paisa becomes `(amount, currency)` and every
comparison and sum needs a currency check — a larger change than it first appears, and one
worth doing properly rather than by adding a column.

Finally, `bigint` caps at ~9.2 x 10^18 paisa. That is approximately Rs. 92 quadrillion and
is not a real limit, but an unbounded `SUM()` over a partitioned usage table can overflow
an intermediate if someone sums paisa-per-request across all customers for all time. Sum
within a period.

## Consequences for other owners

`billing-domain` blocks any change introducing a float into a money path. `data-model` uses
`bigint` for money columns, never `numeric` or `float`. `test-engineer` asserts exact
integer equality — a tolerance in a money assertion is itself the defect. `hot-path`
formats to rupees only at the response boundary.
