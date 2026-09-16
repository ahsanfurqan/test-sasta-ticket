# ADR-0005: A versioned price list is the pricing primitive; plans are shared lists

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** data-model, billing-domain
- **Resolves:** open question #7

## Context

Commercial wants to give a negotiated customer a different monthly fee, a different
included amount, or different prices, without a deployment — and separately expects to add
plans and change prices over time, with the hard requirement that a price change must never
alter what a customer was already charged in the past.

Those are the same requirement seen from two angles. Both are asking for pricing to be data
with a history, rather than code with a deployment.

This decision blocks the schema: invoices reference the prices they were computed from, and
migrating that reference later means migrating rows that Finance has already sent to
customers.

## Decision

**A versioned price list is the unit of pricing.** A price list carries a monthly fee, an
included quantity, and an ordered set of marginal bands. Every customer points at a price
list version for a given period.

"Starter", "Growth" and "Scale" are not special: they are price lists that many customers
share. A negotiated deal is a price list with one customer on it. There is no override
mechanism, because there is nothing to override.

Price lists are **immutable once referenced**. Changing a price creates a new version;
existing customers are moved to it deliberately. A charge cites the exact price list
version it was computed from, which is what makes a past charge stable by construction
rather than by remembering to be careful.

## Alternatives considered

- **Overrides on a named plan.** The customer stays on "Growth" with specific fields
  overridden. Faster to build, and keeps a literal `plan` column for reporting. It fails
  when a deal needs a differently *shaped* ladder — three bands where the plan has two —
  because an override mechanism has to express "replace the band structure", at which point
  it is a price list wearing a disguise. It also leaves two ways for a price to reach the
  rating function.
- **Plan reference plus optional overrides.** Flexible on paper. In practice it means two
  code paths through the most important calculation in the system, and both have to stay
  correct forever. Rejected on the grounds that the rating function should have exactly one
  kind of input.
- **Prices in code with a deployment per change.** Explicitly rejected by the brief.

## What it costs

- **"What plan is this customer on?" becomes a lookup, not a column.** Reporting, support
  tooling and any "all Growth customers" query need a join through the price list. Mitigated
  by giving shared lists a stable, human-readable name and keeping that name on the version.
- **More rows, and more versions to reason about.** A price change creates a version rather
  than an `UPDATE`, so the pricing tables only ever grow.
- **Moving customers between versions becomes an explicit operation** that somebody has to
  perform and that needs an audit trail. With overrides it would have been an `UPDATE`;
  that is precisely the ease we are giving up, on purpose.
- **Immutability has to be enforced, not assumed.** A referenced price list version must be
  protected at the schema level, because the day someone "just fixes a typo" in a band price
  is the day a past invoice stops being reproducible.

## Where it breaks

This model assumes a charge is a function of *quantity* and a price list. It holds for
per-request pricing. It starts to strain when pricing needs dimensions the list does not
have — per-endpoint pricing, per-region pricing, committed-use discounts, or minimum spend
commitments. Each of those adds an axis, and a price list with several independent axes
becomes a pricing engine, which is a much larger thing than this.

It also strains if customers ever need a price list that changes *within* a billing period
for reasons other than a plan change. The period/version pairing assumes one price list per
customer per segment; anything finer needs a different shape.

## Consequences for other owners

`data-model` builds price lists and versions as the pricing tables, with schema-level
protection against mutating a referenced version. `billing-domain` writes `rate()` to accept
a price list version and nothing else — no plan lookup, no override merging, no branching on
customer type. `pipeline` records the price list version on every charge it computes.
