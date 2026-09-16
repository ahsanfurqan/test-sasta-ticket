# ADR-0011: When Redis is unavailable, the API fails closed

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** hot-path
- **Resolves:** open question #8

## Context

Redis holds the usage counters and the precomputed spending-limit thresholds from ADR-0008.
If it is down, or has restarted with an empty keyspace on the 19th of the month, the hot
path can neither record usage into its buffer nor check a limit.

Postgres is the system of record, so the billing data already written is not at risk. A
fallback that batches usage writes directly to Postgres is technically feasible — Postgres
handles this volume comfortably via `COPY`. So the question is not "can we still bill?", it
is "should we keep serving while we cannot enforce a limit?"

Three requirements collide here: no request goes unbilled, a spending limit must actually
stop traffic, and the customer is paying for an available API. Something has to give, and
this ADR says which.

## Decision

**The API fails closed.** When Redis is unavailable, requests are refused with `503` and a
`Retry-After` header. Availability is the requirement that gives.

No fallback write path to Postgres is built. If we cannot record usage and cannot enforce a
limit, we do not serve the request.

Refused requests are not billed (ADR-0007), so refusing preserves "no request goes unbilled"
by construction: there is no served-but-unrecorded state to reconcile later.

### The property this buys

ADR-0008's overshoot budget — a customer exceeds their limit by at most ~5 seconds of
traffic — is only meaningful while Redis is up. Under any fail-open variant, a Redis outage
creates an unbounded window in which we serve past a limit without knowing it, and the
stated budget quietly becomes a lie.

Failing closed makes that bound **unconditional**. There is no state in which the system
serves a customer past their spending limit unknowingly. That is a materially stronger
guarantee than "5 seconds, except during incidents", and it is the main argument for this
decision.

## Alternatives considered

- **Keep serving via a Postgres fallback, and cap the invoice at the limit.** Keeps the
  customer's API up and absorbs any overage as our cost, since our infrastructure failed.
  Genuinely attractive, and the recommendation that was put forward. Rejected on two
  grounds: a fallback write path executes only during an outage, which makes it the
  least-exercised code in the system running at precisely the worst moment; and it needs an
  in-process buffer to batch writes, which trades a clean failure for a messy one where a
  process death loses an unknown quantity of usage.
- **Keep serving via the fallback and bill normally.** Maximum availability and no revenue
  given up, but the customer is billed past a limit they explicitly set because our Redis
  fell over. Indefensible on a support call.
- **Refuse only customers who set a spending limit.** Honours stated intent precisely and
  keeps everyone else serving. Rejected for complexity in exchange for a partial guarantee:
  it still needs the fallback write path for the customers who *are* served, so it pays the
  main cost of fail-open while only partly buying the benefit of fail-closed.

## What it costs

This is the expensive choice, and the cost should not be softened:

- **A Redis blip becomes a full outage for every customer**, including the majority who
  never set a spending limit and would happily have kept being served. We are taking down
  paying customers to protect a guarantee most of them did not ask for.
- **Redis moves from cache to hard dependency.** Describing it as "a cache and a buffer,
  never the system of record" remains true for *data*, but not for *availability* — the
  service is now exactly as available as Redis. That deserves HA, replication and a fast
  restart path, none of which exist yet.
- **Our availability SLO is now bounded by Redis's**, which is a commitment nobody has
  reviewed commercially.
- **Empty-keyspace restart is not distinguishable from "no usage yet"** without care. A
  Redis that comes back healthy but empty will happily serve requests against a zero
  counter, which is worse than being down. Counters must be rebuilt from Postgres and marked
  authoritative *before* traffic is accepted — otherwise fail-closed protects nothing in the
  exact scenario it was chosen for.

## Where it breaks

The last point above is the sharpest edge and is not yet solved: **rebuild time from
Postgres is the number that matters and is currently unknown.** If rebuilding a month of
counters for all customers takes minutes under load, then a Redis restart means minutes of
full outage, and the availability cost is far higher than "a blip". That number needs
measuring before this design goes live, and if it is bad, this decision should be revisited
rather than defended.

This also breaks down commercially at scale. Taking down a large customer's integration
because of our own infrastructure is the kind of incident that ends contracts, and at some
point the business will prefer absorbing overage to explaining an outage. The natural
evolution is a per-customer policy — fail open for customers with no limit, closed for
customers with one — which is the rejected third option arriving later with real incident
data behind it.

## Consequences for other owners

`hot-path` returns `503` with `Retry-After` when Redis is unreachable, distinguishes
"unreachable" from "reachable but empty", and refuses traffic until counters are marked
authoritative. `pipeline` owns rebuilding counters from Postgres after a restart, marking
them authoritative, and must be able to state how long that takes. `test-engineer` proves
the empty-keyspace case specifically — a Redis that restarts clean mid-month must not serve
a single request against a zero counter.
