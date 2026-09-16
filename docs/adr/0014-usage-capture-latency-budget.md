# ADR-0014: Usage capture may add at most 1ms at p99

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** hot-path
- **Resolves:** open question #9

## Context

The brief says recording usage "must not add noticeable delay". That is not testable.
`hot-path`'s standing question — *what did this add to p99?* — has no threshold to fail
against without a number, so every optimisation argument stays an opinion and every
regression is a judgement call.

## Decision

**Usage capture and limit enforcement together may add no more than 1ms at p99**, measured
as the difference between the endpoint with capture enabled and the same endpoint with it
disabled, at a stated concurrency.

This is a budget to design against, not a benchmark to hit on this laptop. It is enforced in
review — a change that cannot answer "what did this add to p99?" does not merit an
exception — and measured by the load harness with capture toggled off and on.

The budget covers the whole per-request billing cost: the counter operation, the threshold
comparison, and the handoff of the usage event. It deliberately does *not* cover the Redis
round trip's behaviour under Redis degradation, which is ADR-0011's territory.

## Alternatives considered

- **5ms.** Comfortable, and easy to hold even with a naive implementation. Rejected because
  on a trivial endpoint 5ms is a large share of the total response time — the customer is
  paying for a fast API, and a budget that any implementation passes is not a constraint.
- **10ms.** Effectively unlimited at this workload. It would become a rubber stamp.
- **No fixed budget, measure and report.** Honest about the uncertainty, and avoids a number
  chosen without production data. Rejected because it leaves us exactly where the brief did:
  with an adjective instead of a threshold, and no way to fail a diff.

## What it costs

- **1ms is tight enough to constrain the design, which is the point and also the cost.** It
  rules out a second network round trip on the request path, rules out synchronous rating,
  and means the counter operation and any event handoff must be pipelined into a single
  Redis interaction rather than issued separately.
- **It requires measurement infrastructure to be meaningful.** A budget nobody measures is a
  comment. The load harness must be able to run with capture disabled to produce the
  baseline, which is a mode that exists only for this purpose.
- **The number is a guess.** It is chosen from what the operations plausibly cost, not from
  production data we do not have. It may turn out to be too tight under real Redis latency
  at real concurrency, and revising it is a new ADR rather than a quiet adjustment.

## Where it breaks

p99 measured on a laptop with a local Redis over loopback tells you almost nothing about p99
with Redis across a network under load, where tail latency is dominated by queueing rather
than by the operation. The budget will be easy to meet locally and may be hard to meet in
production — which is the wrong way round for a useful constraint, and worth stating plainly
rather than discovering later.

It also breaks under ADR-0007's requirement that capture happens *after* the handler runs.
If the handler's response has already been committed to the wire, capture time may not be
visible in the client's measured latency at all, but it still occupies the worker. The
budget then constrains throughput rather than latency, and p99 stops being the right metric.
This is a real tension between ADR-0007 and this one, and the measurement method has to
account for it.

## Consequences for other owners

`hot-path` owns the budget and answers for it on every diff. `test-engineer` builds the
capture-disabled baseline mode into the load harness, and reports p99 with and without, at a
stated concurrency, never as a bare number. `pipeline` must ensure nothing it does — buffer
flushes, threshold recomputation — creates latency on the request path.
