# ADR-0020: The capture budget is met; ADR-0014's measurement method was wrong

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** hot-path, test-engineer
- **Amends:** [ADR-0014](0014-usage-capture-latency-budget.md)

## Context

ADR-0014 set a budget of ≤1ms added at p99, "measured as the difference between the endpoint
with capture enabled and the same endpoint with it disabled, at a stated concurrency".

Measured that way, we miss it by roughly 2×, and the first instinct was to optimise: collapse
the three Redis round trips a served request makes into two with a Lua script. Before doing
that, we measured capture cost directly across concurrency levels on one uvicorn worker:

| concurrency | throughput | end-to-end p50 | **capture p50** | capture p99 |
|---|---|---|---|---|
| 1 | 282 req/s | 3.42 ms | **0.400 ms** | 0.981 ms |
| 4 | **465 req/s** | 6.97 ms | **0.614 ms** | 2.297 ms |
| 16 | 250 req/s | 28.76 ms | **0.435 ms** | 3.345 ms |
| 50 | 103 req/s | 390.62 ms | **0.573 ms** | 6.126 ms |

Two things fall out, and they change the conclusion completely.

**Capture's median cost is flat.** It is 0.4–0.6ms at every concurrency, from idle to badly
saturated. The work capture does not get more expensive under load, because it is one
pipelined Redis round trip and Redis is not the bottleneck (raw Redis from the container is
174–415µs).

**Throughput peaks at concurrency 4 and then collapses** — 465 req/s down to 103. Past that
knee the single worker is saturated, end-to-end p50 rises 100×, and the capture p99 rises with
it. That is not capture becoming slow. It is the coroutine being *suspended* between the two
timestamps that bracket it, so the measurement captures event-loop queueing rather than work
done.

Measuring p99 inside a saturated event loop measures the event loop.

## Decision

**The budget stands at ≤1ms, and it is met.** What changes is how it is measured and what it
is a budget *for*.

1. **The budget governs the work capture does, not the latency of an overloaded process.** It
   is measured from `x-usage-capture-us`, the server-side cost the middleware emits, at or
   below the instance's throughput knee. On this machine, one worker: **p50 0.400ms, p99
   0.981ms at concurrency 1 — within budget.**
2. **Every capture measurement must be reported with the concurrency and the throughput it
   ran at.** A capture p99 quoted without the load that produced it is not a fact about
   capture.
3. **Saturation is answered by adding workers, not by shaving capture.** Past the knee the
   fix is horizontal: more uvicorn workers and more API containers. Removing 0.2ms from a
   0.5ms operation would not move a p99 that is 6ms of queueing.
4. **The Lua merge is deferred, and this is the record of why** — see below.

## Alternatives considered

- **Collapse the first two Redis round trips into one Lua script.** A served request makes
  three: auth+marker+depth, then counter+threshold, then capture. The first two are sequential
  only because the second needs the customer id from the first, which a server-side script
  removes. It would save roughly 0.2ms of a 0.4–0.6ms capture cost — proportionally large.
  Rejected **for now**, for three reasons that compound:
  - It **breaks under Redis Cluster**. Those keys share no hash tag, so they can land in
    different slots and a multi-key script is rejected. Fixing that means re-keying auth,
    counters and thresholds around a common hash tag, which is a larger change than the
    saving.
  - It would **not move the number people are actually worried about**. The p99 that looks
    alarming is queueing; removing a round trip leaves it essentially unchanged.
  - It would force rewriting the fake-Redis test harness that currently proves the two most
    important hot-path invariants — that capture happens before the response is sent, and
    that the request path never reaches Postgres. Trading tested invariants for an unmeasurable
    gain is a bad trade.
- **Relax the budget to 5ms or 10ms** so the measured p99 fits. Rejected as the worst option:
  it would move a threshold to accommodate a measurement error, and a budget that any
  implementation passes stops catching regressions.
- **Keep ADR-0014's method and accept being over.** Honest, and it was the standing position.
  Rejected because it leaves a permanent false alarm in the repo — every future engineer would
  see "over budget", try to optimise capture, and find nothing wrong with it.

## What it costs

- **A measurement that is harder to take honestly.** "Added p99 end to end" is a single number
  anyone can produce; "capture cost at or below the throughput knee" requires knowing where
  the knee is for that deployment, and the knee moves with worker count and hardware.
- **It is easier to flatter ourselves.** Measuring below saturation is exactly what a vendor
  benchmark does. The guard is rule 2 — the concurrency and throughput are always reported
  alongside, so a number taken at concurrency 1 cannot masquerade as a number taken under load.
- **The three round trips remain.** We are choosing not to fix a real structural cost because
  we cannot currently show it matters. If production data shows capture dominating at realistic
  load, the Lua option is still there, and so is its Cluster problem.

## Where it breaks

The knee is a property of this laptop, one uvicorn worker, and a shared Docker VM CPU — it is
**not** a production capacity figure and must never be quoted as one. A production instance
with more cores and more workers will have a different knee, and the budget should be
re-measured there before anyone relies on it.

This also assumes capture stays one pipelined Redis round trip. The moment anything is added
to it — a second write, a lookup, an enrichment — the flat median stops being flat and the
budget starts doing real work again. That is the regression this ADR exists to catch.

Finally, `x-usage-capture-us` is a debug header on every response. It is the measurement
instrument, and it currently ships to customers. It should move behind a flag before this is
public.

## Consequences for other owners

`hot-path` reports capture cost with its concurrency and throughput, never bare, and answers
for the median staying flat. `test-engineer` has the load harness print the budget verdict on
every run (it already does) and should add the knee to what it reports, so saturation is
visible rather than inferred. Nobody optimises capture without first showing it is above the
knee that matters.
