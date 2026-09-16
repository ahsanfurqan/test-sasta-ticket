# ADR-0002: FastAPI for the customer-facing API, not Django

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** hot-path

## Context

The customer-facing API is the hot path: a few thousand requests per second by design, with
a hard requirement that recording usage adds no noticeable delay. Per-request overhead in
the framework is paid on every single billable request, and it is overhead we can never
recover downstream.

Django is the obvious counter-proposal, and not a weak one. It brings a mature ORM, a
migration system, and — most relevantly — an admin interface, which is exactly the kind of
tooling Support and Commercial will eventually want for looking up a customer's plan,
inspecting an invoice, and answering "why does this line say what it says".

## Decision

FastAPI on uvicorn for the customer-facing API.

The work on the request path is I/O-bound (a cache lookup, a counter operation, a
non-blocking handoff) and async suits it. The request path carries no ORM, no template
layer, and no middleware stack we did not choose deliberately.

## Alternatives considered

- **Django + DRF.** The admin is a genuine asset for the Support requirement, and the
  migration story is better out of the box. It loses on the hot path: a heavier per-request
  stack for a service whose endpoint does almost nothing, and an ORM sitting next to a
  latency budget is a permanent temptation to make a synchronous query. The brief also
  explicitly says no admin screen is needed, which removes Django's strongest card from the
  table for this exercise.
- **Django for admin + FastAPI for the hot path.** Genuinely attractive, and the likely
  end state in production: Django admin over the same Postgres for Support, FastAPI serving
  customers. Rejected *for now* as two deployables, two dependency trees, and two ways to
  write a query in a one-day exercise. Revisit when Support tooling becomes real work.
- **Starlette alone / raw ASGI.** Marginally less overhead, but we lose request validation
  and OpenAPI for a saving that will not show up next to a Redis round trip.
- **Not Python.** Out of scope; the brief expects Python and the team writes Python.

## What it costs

We hand-roll what Django would have given us free:

- No admin. Support explainability must be built as a deliberate API surface rather than
  falling out of a framework — arguably better, since the brief demands the *system*
  explain a charge, but it is work we are choosing to do.
- No ORM, so no free query building. See ADR-0003.
- Migrations need wiring up by hand (Alembic).
- Async discipline becomes a correctness concern: one blocking call in an async handler
  stalls the event loop, and it will not show up in a single-request test. This is a real
  ongoing tax, and it is `hot-path`'s to police.

## Where it breaks

This choice starts looking wrong when the internal/admin surface grows faster than the
customer-facing one — when Support, Commercial pricing overrides, and finance tooling are
the bulk of the work and the hot path is a stable, small thing. At that point the right
move is the split option above, not a rewrite.

It also breaks if the request path ever genuinely needs relational work per request. The
answer there is to fix the design rather than the framework: no ADR makes a synchronous
join affordable at a few thousand req/s.

## Consequences for other owners

`data-model` owns migrations by hand (ADR-0003) and provides repositories rather than
models. `hot-path` owns keeping the request path free of blocking calls. Support
explainability is a first-class deliverable for `billing-domain`, not a byproduct.
