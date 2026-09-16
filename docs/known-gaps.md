# Known gaps

Everything not finished, everything we are unsure about, and everything we would want
answered before this went live. The brief is explicit that a complete system whose numbers
nobody can vouch for is worse than one with two honestly documented gaps — so this document
is not a formality, and nothing below is softened.

---

## Not built

### Credit notes — corrections to an issued invoice

**Status:** policy decided ([ADR-0013](adr/0013-invoice-corrections-by-credit-note.md)),
mechanism absent.

Invoice immutability is enforced in the database and proven against seven direct attacks. But
"an issued invoice can never be edited" plus "invoices will eventually be wrong" implies a
correction document, and there isn't one. Today a discovered error is handled by Finance
outside the system, with no audit trail beyond whatever they write down.

**This is a real operational risk, accepted knowingly.** The first billing bug becomes a
manual process under time pressure.

### Immediate API key revocation

Revocation takes effect within 30 seconds, bounded by the auth cache TTL
([ADR-0015](adr/0015-api-key-model.md)). Fine for routine rotation; unacceptable for an
active compromise, and the first customer to report a leaked key will focus on exactly this.
During a Postgres outage it stretches further still
([ADR-0019](adr/0019-auth-during-a-postgres-outage.md)).

### Per-customer billing timezones

Everything is Asia/Karachi ([ADR-0009](adr/0009-billing-boundaries-in-asia-karachi.md)). The
customer table carries a timezone column from the start so the migration is possible, but it
is not settable. A customer in London gets a month boundary at 20:00 their time.

---

## Built but wrong, or unverified

### The latency budget: resolved, but the measurement is easy to take badly

**Previously listed here as "missed by roughly 2×". It is not.**
[ADR-0020](adr/0020-measuring-the-capture-budget.md) measured capture cost across concurrency
on one worker:

| concurrency | throughput | capture p50 | capture p99 |
|---|---|---|---|
| 1 | 282 req/s | 0.400 ms | 0.981 ms |
| 4 | **465 req/s** | 0.614 ms | 2.297 ms |
| 16 | 250 req/s | 0.435 ms | 3.345 ms |
| 50 | 103 req/s | 0.573 ms | 6.126 ms |

Capture's **median is flat** from idle to badly saturated, and throughput peaks at concurrency
4 then collapses. The rising p99 is the coroutine being suspended between the timestamps that
bracket it — event-loop queueing, not capture work. ADR-0014's method (added p99 end-to-end
under load) measured the event loop.

**What remains a gap:** a served request still makes **three Redis round trips**, and we chose
not to collapse them. The Lua merge would save ~0.2ms of a 0.5ms operation, break Redis Cluster
compatibility (no shared hash tag), and force rewriting the fake-Redis harness that proves
capture-before-ack and no-Postgres-on-the-hot-path. Deferred deliberately — but if production
shows capture dominating, that is the lever.

**Also:** `x-usage-capture-us` is the measurement instrument and currently ships on every
customer response. It should go behind a flag before this is public.

### A Redis process crash can still lose about a second of usage

Redis runs `appendonly yes`, which fsyncs once a second. So the guarantee is precisely *"no
request is lost to an **application** crash"* — and **must never be quoted without that
qualifier**. At a few thousand req/s that residual is a few thousand requests.

Closing it means `appendfsync always` (latency on every request) or synchronous Postgres acks
(the thing this design exists to avoid). Neither is worth it yet, but the claim has to be
stated with its limit.

### Counter rebuild is measured; Redis HA is not built

Rebuilding counters from Postgres takes **312ms for 5,502 customers / 1.1M requests**, linear
in customers. That is far better than
[ADR-0011](adr/0011-fail-closed-when-redis-unavailable.md) feared and materially strengthens
the fail-closed decision.

What is *not* addressed: we fail closed on Redis, so **Redis is a hard availability dependency
with no replication or HA in this deployment**. A Redis blip is a full outage for every
customer, including the majority who never set a spending limit. That was chosen deliberately
and it is still the largest single-component bet in the design.

### Postgres availability and Redis memory are coupled invisibly

During a Postgres outage the usage stream grows to its bound, then the API fails closed
([ADR-0018](adr/0018-capture-before-ack-and-the-failure-matrix.md)). So a long Postgres outage
becomes a total outage — correct at the bound's edge, but **neither component's dashboard
shows the coupling**. The bound itself is a configuration guess with no production data
behind it.

---

## Not tested

### The CLI has no test coverage

`meter.pipeline.cli` is the operational entry point for every manual trigger — drain,
reconcile, close, rebuild counters. It has no tests. This is not theoretical: a refactor
broke its imports and **nothing in the suite caught it**; it was found by running it by hand.

### The real-time revocation test is skipped by default

`test_revocation_takes_effect_within_thirty_seconds_in_real_time` waits out the actual 30s
TTL, so it runs only with `REVOCATION_WINDOW_TEST=1`. It passes (32.5s) but does not run in
`make test`.

### Load is proven at laptop volume, not at the design target

The harness proves 2,000 requests at concurrency 50 are billed exactly once, in three
independent places. The design target is a few thousand **per second**. Correctness at the
volume we can generate is proven; behaviour at the target is reasoned about, not measured.

### The stale-auth path runs only during an outage

[ADR-0019](adr/0019-auth-during-a-postgres-outage.md)'s stale-while-error path is exercised by
`make test-outage`, which genuinely stops Postgres — but that is a host-driven shell script
outside `pytest`, so it does not run in `make test` and will rot if nobody remembers it.

---

## Questions we would want answered before going live

1. **How long is a Redis outage, realistically?** Fail-closed converts it directly into
   downtime. The decision is defensible at seconds and indefensible at hours, and nobody has
   the number.
2. **Does Commercial accept losing roughly Rs. 75,000 per mid-month plan change?** That
   revenue was being collected by omission
   ([ADR-0017](adr/0017-prorate-band-widths-and-partial-periods.md)). Correcting it was right,
   but it is a revenue change somebody should sign rather than discover in a report.
3. **What is the real acceptable overshoot on a spending limit?** We designed to ~5 seconds
   because the brief said customers complain about being cut off "well after". If 30 seconds
   were acceptable, a good deal of background machinery becomes unnecessary.
4. **Is a customer's own `4xx` genuinely billable?** We decided yes
   ([ADR-0007](adr/0007-billable-request-definition.md)), and it is the single decision most
   likely to generate disputes. The exact status-code list needs maintaining deliberately —
   `3xx` currently has no outcome value and is treated as non-billable.
5. **How long must a charge stay itemisable?** Per-request rows expire at 90 days
   ([ADR-0016](adr/0016-usage-retention-and-partitioning.md)). After that a charge is
   explainable but not itemisable, and a rollup bug found on day 91 is unrecoverable. That is
   a compliance question as much as a technical one.
6. **What is the SLO?** Redis is in the critical path for availability, latency and
   enforcement. Our availability is now bounded by its, and nobody has reviewed that
   commercially.
