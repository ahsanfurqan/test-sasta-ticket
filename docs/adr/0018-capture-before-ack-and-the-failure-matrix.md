# ADR-0018: Capture before the response is sent, and what every failure costs

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** hot-path, pipeline
- **Refines:** [ADR-0007](0007-billable-request-definition.md), [ADR-0011](0011-fail-closed-when-redis-unavailable.md), [ADR-0014](0014-usage-capture-latency-budget.md)

## Context

The brief asks directly what happens if the path from a served request to a billable record
fails partway. Scattered answers exist across ADR-0007, 0011 and 0014; no single document
states them together, and the most important one was acknowledged without ever being decided.

ADR-0007 requires usage capture to happen after the handler runs, because billability
depends on the response status. Its "where it breaks" section notes that this creates a
window in which a request is served but not captured, and hands the problem to `hot-path`
and `pipeline` without bounding it.

Two things make that window worse than it first appears:

1. **It is not one request.** An async worker holds many concurrent in-flight requests. A
   `SIGKILL` loses every request that has been answered but not yet captured — bounded by
   worker concurrency, not by one.
2. **ADR-0014's ≤1ms p99 budget pushes toward batching captures in-process**, and the moment
   capture is batched the loss window becomes the entire unflushed batch — thousands of
   requests, not hundreds.

So the latency budget and "no request may go unbilled" pull against each other. This ADR
rules on that, and then states the full failure matrix in one place.

## Decision

### 1. Capture before the response is sent

Usage is written to Redis **after the handler produces a response and before that response is
sent to the customer**:

```
handle request -> status known -> XADD usage to Redis -> send response
```

ASGI middleware sits exactly at that point: it awaits the handler, inspects the outcome,
captures, and only then returns the response for transmission.

**This eliminates the loss entirely, rather than bounding it.** If the process dies before
the `XADD`, it also died before the customer received an answer — so the customer retries,
and that retry *is* the request. If it dies after, the usage is captured. No acknowledged
request is ever unbilled.

### 2. No in-process batching of captures

Capture is one `XADD` per request. Batching in the API process would reopen exactly the
window this decision closes, and it is the tempting optimisation, so it is forbidden here
rather than left to judgement. Batching belongs downstream, in the drain, where a crash is
recoverable.

### 3. Redis Streams with consumer groups as the buffer

The drain reads via a consumer group. A batch is acknowledged (`XACK`) only after the
corresponding rows are committed to Postgres. A worker that dies mid-drain leaves its batch
pending, and it is redelivered on restart; the idempotency key makes redelivery safe.

### 4. Postgres unavailable: keep serving, bound the buffer

Unlike Redis (ADR-0011), Postgres being down does not stop serving. Counting and limit
enforcement both run off Redis, so the system can still do the two things it must not get
wrong. The stream grows until Postgres returns, and the drain resumes.

**The stream is explicitly bounded.** An unbounded stream exhausts Redis memory, which by
ADR-0011 takes the entire API down — turning a recoverable Postgres outage into a total
outage. On reaching the bound the system fails closed, for the same reason ADR-0011 does:
we would otherwise be serving traffic we cannot record. The bound is configuration, and the
alert fires long before it.

### The failure matrix

| Failure | What happens | Cost |
|---|---|---|
| Process dies between handler and capture | Customer never received a response; their retry is the request | **Nothing lost** |
| Process dies between capture and sending the response | Usage captured, customer sees a connection error and retries | The retry is a second served request and is billed (correct: we did the work twice) |
| Worker dies mid-drain | Batch never `XACK`ed, redelivered on restart, idempotency key dedupes | Nothing lost, nothing double-counted |
| Postgres down | Redis keeps buffering, serving and limit enforcement continue, drain resumes | Thresholds go stale (see below); stream grows to its bound |
| Postgres down past the stream bound | Fail closed | Full outage, deliberately, rather than serving unrecordable traffic |
| Redis down | `503`, fail closed (ADR-0011) | Full outage; we can neither count nor enforce |
| Redis restarts empty | Counters rebuilt from Postgres and marked authoritative before traffic is accepted (ADR-0011) | Outage for the rebuild duration |
| Redis crashes with AOF `everysec` | Up to one second of stream lost | **Real residual loss — see below** |

## Alternatives considered

- **Capture after the response is sent (ADR-0007 as implemented so far).** The instinctive
  ordering, and it keeps the `XADD` off the customer's latency path entirely. Rejected: it
  makes "no request goes unbilled" false by design, for a latency saving we do not need. The
  brief lists losing usage as losing revenue and does not list 0.2ms as a concern.
- **Capture before the handler runs.** Zero loss and the cheapest possible path, but the
  status is not yet known, so billability cannot be determined (ADR-0007). It would bill
  `401`s and our own `5xx`s.
- **Write-ahead: record intent before the handler, finalise after.** Eliminates the window
  and preserves status-based billability. Rejected as two writes per request on the hot path
  to solve a problem that ordering solves with one.
- **Fail closed when Postgres is down**, symmetrically with Redis. Rejected: Postgres is not
  needed to count or to enforce, so refusing traffic would be an outage we do not have to
  take. The stream bound is what keeps that judgement safe.

## What it costs

- **One `XADD` on the latency path**, roughly 0.2ms locally. It consumes a fifth of ADR-0014's
  budget before anything else is measured, and it is now the dominant per-request cost.
- **No batching**, so Redis sees one write per request — a few thousand writes per second at
  the design target. Redis handles that comfortably; it does mean Redis capacity scales with
  request rate rather than with batch count.
- **A slower response under Redis latency.** A slow Redis now directly slows every customer
  response rather than degrading quietly in the background. Combined with ADR-0011's
  fail-closed stance, Redis is unambiguously in the critical path for both availability and
  latency. That is a large bet on one component.
- **Thresholds go stale while Postgres is down.** Limit enforcement keeps working against the
  last computed threshold, but a plan change, a limit change, or a new customer cannot be
  reflected. Enforcement degrades slowly and silently, which is the failure mode this project
  dislikes most — it needs an alert on threshold age, not just on Postgres.

## Where it breaks

**The AOF window is the honest residual.** Redis is configured `appendonly yes`, which by
default fsyncs once a second. A Redis process crash can therefore lose up to one second of
captured usage — at a few thousand req/s, a few thousand requests. So the guarantee this ADR
makes is precisely: *no request is lost to an **application** crash*. Requests can still be
lost to a **Redis** crash. Closing that would mean `appendfsync always` (a large latency cost
on every request) or acknowledging into Postgres synchronously (the thing this whole design
exists to avoid). Neither is worth it at this stage, but the claim must be stated with its
qualifier and never as "nothing is ever lost".

The design also assumes the customer retries on a connection error. A fire-and-forget client
that does not retry loses its own request, and we will never know it existed. Nothing can be
done about that from our side, but it means "no request goes unbilled" is a statement about
requests we answered, not about requests that were attempted.

Finally, the stream bound converts a long Postgres outage into a total outage. That is the
right trade at the bound's edge, but it means Postgres availability and Redis memory are
coupled in a way that is not obvious from either component's dashboard.

## Consequences for other owners

`hot-path` captures in ASGI middleware, after the handler and before the response is sent,
with no in-process batching, and owns the `XADD` cost against ADR-0014's budget.
`pipeline` drains via a consumer group, `XACK`s only after the Postgres commit, owns the
stream bound and its alert, and owns an alert on threshold staleness distinct from a Postgres
health check. `test-engineer` proves the matrix above by injection rather than by argument:
kill the API mid-flight, kill the worker mid-drain, stop Postgres, and assert exact totals.
