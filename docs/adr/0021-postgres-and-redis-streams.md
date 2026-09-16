# ADR-0021: Postgres as the system of record, Redis Streams as the hot-path buffer

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** data-model, pipeline

## Context

Postgres and Redis were named in the task description, and this ADR exists because *being
told* is not a reason. The brief is explicit that a different datastore is fine if we can
say why — which means the reverse also holds: keeping these needs a defence, or the design
is unexamined.

The workload is a genuinely awkward shape, and it is exactly the shape people reach for
Kafka and a columnar store to handle:

- **Append-heavy and high-volume.** At the design target, hundreds of millions of usage rows
  a month, written one per request.
- **But summed exactly.** An invoice must be right to the paisa, and once issued it can never
  move. That is a transactional requirement sitting on top of an analytics-shaped write load.
- **And explainable.** Any charge must decompose into quantities, band prices and the price
  list version that produced them — which means joins, referential integrity, and a
  transaction that spans usage, rollups, and invoice lines.

The tension is that the write path wants a log and the read path wants a relational database.

## Decision

**Postgres 16 is the system of record for everything that touches money.** Usage events,
rollups, price lists and their versions, plan assignments, spending limits, invoices and
invoice lines all live there, in one database, under one transaction boundary.

**Redis 7 carries the hot path**: the auth cache, the per-period request counters, the
precomputed spending-limit thresholds, and the usage buffer as a **Stream with consumer
groups**. It is a cache and a buffer, never truth. Losing it entirely must cost nothing but
availability, which is why counters are rebuildable from Postgres (ADR-0011) and why the
stream is drained into Postgres rather than read from.

Two datastores, not three. Redis is already required for the O(1) integer comparison that
makes spending limits affordable (ADR-0008), so using it for buffering too avoids a third
operational dependency for a job it can do at this volume.

## Alternatives considered

- **Kafka (or Redpanda) for the buffer instead of Redis Streams.** The strongest
  alternative, and the right answer at a larger scale. A replicated log is durable by
  default, its retention is bounded by disk rather than memory, and `acks=all` closes the
  ~1s AOF window this design accepts (ADR-0018). Rejected *now* because it would be a third
  datastore to run alongside a Redis we already need, for a buffering job Redis Streams does
  adequately at a few thousand messages per second — consumer groups, a pending list, and
  `XAUTOCLAIM` are precisely the crash-safety primitives the drain leans on. **The signal to
  switch is already instrumented:** when the stream bound (ADR-0018) becomes the binding
  constraint rather than a safety valve, Kafka is the answer.
- **ClickHouse or TimescaleDB for usage events.** Purpose-built for this write volume and
  for aggregating it. Rejected because the invoice must be transactional *with* the usage it
  bills: issuing involves rollups, invoice lines and a period status changing together, with
  foreign keys onto price list versions that must not drift. Splitting usage into a second
  engine puts a system boundary in the middle of reconciliation — and reconciliation proving
  a delta of zero is the load-bearing claim of this whole design. Revisit when 90-day
  per-request retention becomes a storage problem rather than the partition drop it is today;
  the natural end state is Postgres keeping billing truth while a columnar store serves
  analytics, fed from the same events.
- **DynamoDB or Cassandra for usage.** Excellent write scaling and horizontal growth.
  Rejected for the same reason as above, more sharply: no transactional sum, no joins, so
  both exactness and explainability would have to be rebuilt in application code. Rebuilding
  a transaction in application code over a money path is how money goes missing.
- **A managed queue — SQS, RabbitMQ — instead of Redis Streams.** Operationally simpler than
  Kafka. Rejected because a delivered-then-acked queue discards the message: there is no
  replay after an ack, and the reconciliation story depends on being able to re-read. We
  would also still need Redis for counters, so it buys nothing over Streams.
- **Postgres alone, no Redis at all.** By far the simplest thing that could work, and it
  satisfies "no request goes unbilled" trivially, because capture would be a synchronous
  insert inside the request. Rejected on the latency budget: it puts a database write *and* a
  spending-limit query on every request. It also loses the integer-comparison trick entirely
  — ADR-0008's whole point is that enforcement never computes a bill — and makes Postgres
  connection count, not Redis memory, the first ceiling.
- **A queue table in Postgres, or `LISTEN`/`NOTIFY`.** Avoids the second datastore. Rejected
  because a queue table on the hottest write path means the same row churn we are trying to
  keep off it, plus vacuum pressure on a table that is constantly inserted and deleted —
  the classic Postgres-as-a-queue failure. `LISTEN`/`NOTIFY` is not durable across a
  disconnect, which makes it unusable for anything billable.

## What it costs

- **Two systems to operate, with a coupling neither dashboard shows.** During a Postgres
  outage the stream grows toward its bound; at the bound the API fails closed. So Postgres
  availability and Redis *memory* are linked, and you cannot see that from either component's
  metrics alone.
- **Redis is the largest single-component bet in the design.** It is in the critical path for
  availability (fail-closed, ADR-0011), for latency (capture is synchronous, ADR-0018), and
  for enforcement. There is no replication or HA in this deployment.
- **~1 second of usage is at risk on a Redis process crash** — AOF fsyncs once a second.
  Kafka with `acks=all` would not have that window. It is a knowing trade, recorded in
  ADR-0018 and in the known gaps.
- **Partition management is ours.** Postgres does not create next month's partition on its
  own, so that is a scheduled job we own and must not forget.

## Where it breaks

**Redis memory is the first ceiling, and it arrives before Postgres write throughput.** One
`XADD` per request with no batching means buffer capacity scales with request *rate*, not
with batch count. The moment sustained traffic outpaces the drain for long enough to
approach the bound in normal operation — not during an outage — Redis Streams has stopped
being the right tool and Kafka has started.

**Redis Cluster does not fit the current key design.** Auth entries, counters and thresholds
share no hash tag, so they can land in different slots: multi-key operations across them
(including the Lua merge deferred in ADR-0020) are rejected under Cluster. Sharding Redis
therefore requires re-keying first, which is a larger change than it appears.

**Postgres is a single writer.** Read replicas help the live-usage endpoint and nothing else;
the write path, the drain and the invoice run all need the primary. Multi-region, or write
volume beyond one primary, means sharding by customer — and the exclusion constraint on
`plan_assignments` plus cross-table invoice transactions both assume a single database.

## Consequences for other owners

`data-model` owns partition creation and keeps every money column in Postgres. `pipeline`
owns the stream bound, its alert, and the rebuild path that makes losing Redis survivable.
`hot-path` treats Redis as the availability dependency it is and fails closed rather than
serving unrecorded traffic. Nobody adds a third datastore without superseding this ADR.
