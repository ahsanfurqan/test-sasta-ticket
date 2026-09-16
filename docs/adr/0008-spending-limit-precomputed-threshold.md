# ADR-0008: Spending limits enforce against a precomputed request-count threshold

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** hot-path, pipeline
- **Resolves:** open question #3

## Context

Customers can set a spending limit — "stop serving my requests once I reach Rs. 50,000 this
month" — and have said plainly that being cut off well after passing it will generate
complaints. Meanwhile the hot path cannot afford a synchronous billing lookup, and "what do
you owe right now?" is a ladder calculation over a price list, not a cheap read.

Stated that way the two requirements look opposed, which is what makes this interesting.
They are only opposed if the question asked on the request path is "what does this customer
owe?".

## Decision

**Ask a different question.** Convert the customer's rupee limit into **the request count at
which they reach it** — a single integer — and hold that in Redis alongside the request
counter. The hot path compares two integers.

```
if counter >= threshold: refuse
```

No ladder, no price list, no Postgres, no money arithmetic on the request path. The
expensive direction (rupees → requests) is computed in the background by `pipeline`, because
the ladder is monotonic in quantity and therefore invertible: for any spending limit there
is exactly one request count at which it is first reached.

**Overshoot budget: a customer may exceed their limit by at most ~5 seconds of traffic**,
and the budget is a stated target the design is built against, not a number discovered
afterwards.

The threshold is recomputed when anything that moves it changes: the limit itself, the plan
or price list, or a mid-month plan change (which alters the ladder and therefore the
inversion). Recomputation is background work whose latency the customer never sees.

## Alternatives considered

- **Rate the usage on every request.** Exactly correct at every instant, zero overshoot from
  staleness. Rejected outright: a ladder calculation per request, and a price list read, on
  the hottest path in the system.
- **Periodically compute the accrued cost and cache it.** The obvious middle ground, and
  what most people reach for first. It makes the overshoot equal to the refresh interval
  *times the traffic rate*, which is exactly backwards — the busier the customer, the further
  past their limit they get. The threshold inversion has the opposite property: overshoot is
  bounded by concurrency, not by spend rate.
- **Check the limit asynchronously and revoke the key.** Simple, but the gap between
  crossing and revoking is unbounded in the worst case, and key revocation is a much blunter
  instrument than refusing a request.
- **A looser overshoot budget (30s, or minutes).** Cheaper in background work. Rejected
  because at a few thousand requests per second, 30 seconds is tens of thousands of requests
  past a limit the customer explicitly set — the precise complaint the brief quotes.

## What it costs

- **A background recomputation path that must keep up.** The threshold is only as good as
  its freshness, and every input that moves it needs a recomputation trigger. A missed
  trigger is a silent enforcement failure — the worst kind, because everything looks fine.
- **A counter read on every request.** Cheap, but not free, and it is now on the critical
  path. `hot-path`'s standing question applies.
- **Overshoot from in-flight concurrency.** Requests already in flight when the counter
  crosses the threshold still complete. At a few thousand req/s this is a small number of
  requests, not a small number of seconds — and it is irreducible without a synchronous
  check we have already rejected.
- **The inversion assumes the ladder is monotonic in quantity.** It is, for every price list
  in the brief. A price list with a *negative* marginal band — a volume rebate that makes the
  next request reduce the bill — would break the inversion. That should be forbidden at the
  price-list level rather than handled here.

## Where it breaks

**If Redis restarts, both the counter and the threshold are gone.** Enforcement silently
stops until both are rebuilt from Postgres. That is open question #8 (fail open or fail
closed), and this ADR makes answering it more urgent: the failure mode is not just lost
usage, it is unenforced limits. Rebuild time under load is the number that matters, and it
is not yet known.

The ~5 second budget also depends on how quickly usage reaches the counter. If usage capture
is buffered on the hot path for latency reasons, the counter lags by the buffer window, and
the real overshoot is the buffer window plus recomputation lag — not 5 seconds. **These two
decisions are coupled**, and the buffering design must be chosen with this budget in hand
rather than independently.

Finally, the threshold is computed against a *forecast* of the month's shape. A mid-month
plan change moves it, and a customer sitting exactly at a band boundary when their plan
changes is the case most likely to be off by a request or two.

## Consequences for other owners

`pipeline` owns threshold computation and every trigger that invalidates it, and must be
able to state how stale a threshold can be. `hot-path` keeps the check to an integer
comparison, and owns the fail-open/fail-closed behaviour once #8 is decided.
`billing-domain` provides the inversion (rupee limit → request count) as a pure function and
guarantees price lists remain monotonic. `test-engineer` proves enforcement under concurrent
traffic — the overshoot at the moment of crossing, with many requests in flight, is the case
that matters.
