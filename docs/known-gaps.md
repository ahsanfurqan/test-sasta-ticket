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

### Nothing drops old usage partitions

[ADR-0016](adr/0016-usage-retention-and-partitioning.md) retains per-request rows for 90 days
and specifies that expiry is a partition drop rather than a mass `DELETE`. The partitioning
is in place and creation is automatic — the drain calls `ensure_usage_partition` before
writing into a period it has not seen. **The job that drops them does not exist.**

This database still holds partitions back to February:

```
usage_events_2026_02    1184 kB
usage_events_2026_11     638 MB
```

So retention is a design that is ready rather than a mechanism that runs. On a laptop it is
invisible; in production it is the difference between a table that ages out and one that only
grows — and growth on this table is the thing the partitioning was chosen to survive.

The work is small: drop partitions older than the window, after checking no open billing
period depends on them. Two related things are also missing — there is no archival of
expiring detail to cold storage before the drop, so the 90-day boundary is currently a
deletion rather than a tiering, and nobody has decided whether that is acceptable.

### A drifted Redis counter is never repaired while Redis is up

The per-period request counter is incremented in the same Redis pipeline as the usage
capture, so the counter and the stream cannot drift from each other at write time. What has
no repair path is drift that appears any other way — a partially applied pipeline, an `INCR`
that lands while its `XADD` does not, an operator poking the key.

Correction only ever happens through `counters.rebuild()`, which authoritatively `SET`s each
counter from `count(*)` over `usage_events`. And the watchdog only calls it when the
authoritative marker is **missing**:

```python
if await is_authoritative(redis):
    return None      # Redis is up and primed -> no rebuild, ever
```

So the only thing that repairs a counter is a Redis restart.

Reconciliation would *detect* it — that is what `unexplained = (redis − postgres) − undrained`
is for — but `reconcile.py` contains no write of any kind. It reports; it does not repair. And
it only runs at month close or when a human asks (see below).

**Why it matters more than it looks:** the counter is what the spending-limit gate compares
against. Too high cuts a customer off early; too low serves them past a limit they set. Either
way it stays invisible until a period is closed, and at that point it is recorded as a
discrepancy rather than fixed.

The fix is small, because `rebuild()` already does exactly the right thing per customer. What
is missing is a trigger that is not "Redis died" — either a periodic reconcile sweep that
repairs on drift, or letting the existing watchdog re-prime individual counters.

### Counter keys are never expired, in the component that fails closed on memory

Counter keys are per customer per period — `usage:count:<customer>:<YYYY-MM>` — so a new month
creates a new key and the old one is simply left behind:

```
sample key:  usage:count:3c3e59e3-…:2026-12
TTL:         -1          (never expires)
keys now:    8701
```

At 10,000 customers that is 120,000 dead keys a year, growing without bound, in the component
that ADR-0021 names as **the first ceiling this design hits** and that ADR-0011 makes fail the
entire API closed when it runs out of memory.

The current volume is nothing. The shape is the problem: unbounded growth in the exact
resource the design says breaks first, with nothing in the system reclaiming it.

Fix: set a TTL when the key is created, comfortably longer than the period plus the month
close grace window, so a closed month's counters expire on their own. The threshold keys want
the same treatment and have not been checked.

### Reconciliation is not on a schedule

It runs in exactly two places: as the gate before an invoice is issued (`close.py` loops
drain → aggregate → reconcile until it converges or the grace window expires, per ADR-0010),
and on demand via `cli reconcile` or `GET /ops/reconcile/{customer_id}`.

The worker runs five background loops — drain, aggregate, thresholds, watchdog, close — and
**none of them is reconciliation**. So between month closes, nothing is checking that Redis
and Postgres agree.

This is defensible as built: the drain acks only after the commit, the idempotency key makes
redelivery safe, and 14 real process kills lost nothing. Reconciliation is the proof, and it
is demanded at the moment it matters — before money is committed to a document that can never
be changed.

But "we would find out at month end" is a long feedback loop for a silent divergence, and
`unexplained ≠ 0` is exactly the kind of thing worth paging on within minutes rather than
weeks. A periodic sweep over active customers would reuse `reconcile.reconcile` unchanged and
slot in beside the existing `thresholds` loop.

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

### The CLI is only tested for reachability

`tests/integration/test_cli.py` proves every subcommand imports, parses and dispatches — the
class of bug that previously got through, when a refactor broke the CLI's imports and the
whole suite stayed green. It does **not** test what the stages do; that is covered against
the real container by `test_drain`, `test_reconciliation` and `test_invoicing`.

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
6. **Who is watching the counter between month closes?** A drifted counter misenforces a
   spending limit, and today nothing detects or repairs it until a period is closed. Deciding
   how quickly that must be caught decides whether a periodic reconcile sweep is worth its
   cost.
7. **What is the SLO?** Redis is in the critical path for availability, latency and
   enforcement. Our availability is now bounded by its, and nobody has reviewed that
   commercially.
