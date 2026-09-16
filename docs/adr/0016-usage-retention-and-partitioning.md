# ADR-0016: Per-request usage retained 90 days; rollups retained long-term

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** data-model
- **Resolves:** open question #11

## Context

Support must be able to explain any charge on any invoice. That requires keeping enough
detail to re-derive it. The question is how much detail, and for how long.

At a few thousand requests per second the usage table gains on the order of hundreds of
millions of rows per month. Keeping every request forever is a storage and operations
problem that will dominate every other decision in this schema. Keeping nothing per-request
makes "which requests made up this line?" unanswerable.

## Decision

**Two tiers.**

- **Per-request usage rows: 90 days.** Full detail for recent periods — enough to cover the
  invoice a customer is currently disputing and the two before it.
- **Aggregated rollups: long-term.** Per customer, per day, per price list version, per plan
  segment. Small, and sufficient to re-derive every line of any invoice exactly, because a
  charge is a function of quantity, band and price list version — all of which the rollup
  carries.

**The usage table is partitioned by period**, so expiry is a partition drop rather than a
mass `DELETE`. A `DELETE` of hundreds of millions of rows generates dead tuples faster than
autovacuum can reclaim them, on the table with the highest write rate in the system; that is
a predictable outage, not a tuning problem.

Rollups are produced by `pipeline` as part of aggregation and are **not** derived from
per-request rows at query time, so they remain correct after the detail expires.

## Alternatives considered

- **13 months of per-request rows.** Any invoice from the past year explainable down to the
  individual request, including year-on-year comparison. Roughly four times the storage on
  the largest table in the system, for a level of detail that is requested in a small
  minority of disputes. Rejected on cost, but it is the natural first extension if disputes
  turn out to need it.
- **7 years of per-request rows.** Compliance-grade. Tens of billions of rows, mandatory cold
  archival, and an operational burden out of proportion to any stated requirement. If a
  regulator ever requires it, the answer is archival to object storage, not a bigger
  Postgres table.
- **Rollups only.** Smallest footprint, simplest operations, and invoices still re-derive
  exactly. Rejected because it removes the ability to answer "which requests?" at all, which
  is a meaningful weakening of the explainability the brief asks for — and the answer is
  needed precisely when a customer is disputing, which is when it matters most.

## What it costs

- **After 90 days, a charge is explainable but not itemisable.** We can show that 200,000
  requests were charged at Rs. 0.35 under price list version X, and prove the arithmetic,
  but we cannot list them. Support needs to know where that line falls.
- **Rollups must be correct at write time**, because there is no going back to recompute them
  once the detail is gone. A rollup bug discovered on day 91 is unrecoverable for the
  affected period. This raises the stakes on `pipeline`'s aggregation considerably and makes
  the reconciliation test more important than it first appears.
- **The rollup grain is now fixed.** Per customer, per day, per price list version, per
  segment. Any question needing a finer cut — per key, per endpoint, per hour — must be
  decided *before* the detail expires, not after.
- **Partitioning constrains the index set**, since indexes are per-partition and global
  uniqueness across partitions is not free. Idempotency keys in particular need care.

## Where it breaks

The 90-day boundary interacts badly with a **late dispute**. A customer querying a charge
from four months ago gets the rollup explanation, which is correct and complete arithmetically
but may not satisfy someone who believes they were charged for requests they never made.
There is no way to prove a negative from a rollup.

The rollup grain also breaks the day someone asks a question it does not answer. "Which of
my API keys caused this spike in March?" is unanswerable in April if per-key is not in the
grain. The grain should be chosen generously now, because widening it later only helps
future periods.

Finally, this assumes 90 days of per-request data is affordable, which depends on a traffic
volume we have not seen. At the design target it is roughly a billion rows in the retained
window — large but manageable when partitioned. Ten times that would force the decision
again.

## Consequences for other owners

`data-model` partitions the usage table by period, implements expiry as a partition drop,
and chooses the rollup grain deliberately and generously. `pipeline` writes rollups as part
of aggregation rather than deriving them on demand, and owns their correctness at write
time. `billing-domain` re-derives a historical charge from a rollup plus a price list
version, never from per-request rows, so explainability does not silently depend on data
that expires. `test-engineer` proves a rollup and its per-request rows agree exactly before
expiry — because after expiry, nothing can.
