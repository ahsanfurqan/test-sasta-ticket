# Metered Billing API

A paid API product. Customers sign up, get an API key, call our API, and pay for how much
they use. What the endpoint returns is deliberately trivial — none of the interesting work
is in the endpoint. The interesting work is counting correctly, charging correctly, and
being able to explain every rupee.

Source of truth for requirements: the assessment brief. Where the brief is silent, see
`docs/open-questions.md` — do not quietly resolve an open question in code.

## The five capabilities

1. **Authenticated serving** — serve API requests, authenticated by customer API key.
2. **Usage recording** — record how much each customer used. No request goes unbilled.
3. **Live usage/cost** — an endpoint showing usage this month and what it will cost.
   Near-current, not a day behind.
4. **Spending-limit enforcement** — stop serving a customer who has hit their limit, soon
   after they hit it.
5. **Monthly invoice** — exact to the rupee, immutable once issued, and every charge on it
   must be explainable by the system rather than by a human.

## Pricing model

Tiered **by band, marginal, not retroactive**. Crossing into a cheaper band does not
re-price what came before it. Prices live in **data**, versioned, so that changing a price
today cannot alter a charge computed yesterday.

| Plan | Monthly fee | Included | Beyond included |
|---|---|---|---|
| Starter | Rs. 0 | 10,000 | Rs. 0.80 each |
| Growth | Rs. 15,000 | 500,000 | next 500,000 @ Rs. 0.50, then Rs. 0.35 |
| Scale | Rs. 90,000 | 5,000,000 | next 5,000,000 @ Rs. 0.25, then Rs. 0.15 |

Worked example (Growth, 1,200,000 requests): 15,000 fee + 0 (first 500k included)
+ 500,000 x 0.50 = 250,000 + 200,000 x 0.35 = 70,000 → **Rs. 335,000**.

## The money rule — non-negotiable

**Money is integer paisa (bigint) end to end. No floats anywhere near a charge.**

- 1 rupee = 100 paisa. Rs. 0.80 is `80`, Rs. 15,000 is `1_500_000`.
- All three per-request prices are exact in paisa (80, 50, 35, 25, 15), so per-request
  rating requires no rounding at all. Division only enters through proration — which is
  precisely why the proration policy (open question #1) determines the rounding policy,
  and not the other way round.
- `float`, `Decimal`-to-`float`, and division that silently truncates are all defects in a
  money path, not style preferences. If a number can appear on an invoice, it is an `int`.

## The constraint tensions

These pull against each other. Recognising which ones conflict, and deciding what gives,
is the core of the task. Every agent holds all of these in mind.

- **Recording usage cannot add latency to the customer's request, but no request may go
  unbilled.** Durability wants a synchronous write; latency forbids it.
- **Usage display must be near-current; invoices must be exact to the rupee.** These are
  two different reads of the same underlying truth, with different tolerances.
- **Spending limits must stop traffic soon after the limit is hit, without a synchronous
  billing lookup on the hot path.** "Soon" has to become a number we can state and defend.
- **Prices live in data and are versioned** — a price change must never alter a past charge.
- **An issued invoice is immutable.** Its amount never moves after Finance sends it.
- **Mid-month plan changes need a proration policy Support can explain** to a customer who
  is annoyed, on the phone, looking at one line of an invoice.
- **Target is a few thousand req/s by design, not by benchmark on this machine.** Prove
  correctness at whatever volume this laptop can generate; state where the design breaks.

## Layout and ownership

Each directory has exactly one owning agent (see `.claude/agents/`). Cross-boundary
changes get discussed, not made unilaterally.

```
src/meter/
  api/         hot-path      auth, request serving, usage capture, limit enforcement
  domain/      billing-domain PURE pricing math. No I/O. Ever.
  storage/     data-model    postgres/redis access, repositories
  pipeline/    pipeline      buffering, aggregation, reconciliation, invoice job
  ops/         hot-path      health/readiness
migrations/    data-model    alembic, hand-written SQL
tests/         test-engineer
loadtest/      test-engineer
```

### The seam that matters

`src/meter/domain/` imports **nothing** from `api/`, `storage/`, or `pipeline/`, and
nothing that performs I/O. It is pure functions over integers.

This is enforced mechanically by import-linter in `make lint`, not by discipline. It is
what makes the pricing math property-testable, and what makes "explain this charge" a pure
re-derivation from stored inputs rather than archaeology across three systems.

## Design principles, applied when they earn it

YAGNI, KISS, DRY and SOLID are used here as tie-breakers, not as decoration. Where they
conflict — and they do — the constraint tensions above win, because they come from the
brief and the principles do not.

- **YAGNI.** `meter/domain/` is empty on purpose, and the plan/price tables are unwritten,
  because open question #7 has not been answered. A model written before the question is
  settled is a model written twice. This is also why ADR-0003 chose an ORM: hand-rolling a
  data-access layer to avoid an overhead nobody has measured is speculative optimisation.
- **KISS.** One image for the API and the worker. One database URL. One place a price is
  defined. The complexity budget in this system is spent on the fast-versus-exact gap,
  because that is where the problem actually is — spending it anywhere else is a loss.
- **DRY, about meaning rather than text.** The rating function exists once, and `pipeline`
  calls it rather than inlining "just this one multiplication". Two implementations of the
  same charge will disagree eventually, and the disagreement will be found by a customer.
  Note the limit: incidental similarity is not duplication, and merging two things that
  merely look alike couples them for no reason.
- **SOLID, mostly D and S.** Each package has one owner and one reason to change. The
  domain depends on no infrastructure, which is the dependency-inversion rule doing real
  work: it is what makes rating testable without a database and re-derivable for a
  historical charge. The open/closed idea shows up as prices living in versioned data, so
  a new plan is a row rather than a deployment.

The failure mode to watch for is ceremony: an interface with one implementation, an
abstraction layer over a library we will never swap, a base class that exists to satisfy a
principle. If a principle is producing indirection nobody needs, it is being misapplied.

## Commands

```
make up          # build + start postgres, redis, api, worker; waits for health
make down        # stop and remove containers (add VOLUMES=1 to drop data)
make migrate     # alembic upgrade head
make test        # pytest inside the api container
make lint        # ruff + import-linter (enforces the domain purity seam)
make load-test   # local traffic harness against /v1/echo
make logs        # tail all services
make psql        # psql shell into postgres
make redis-cli   # redis-cli shell
```

API is on `http://localhost:8000`. Auth is `X-API-Key`. The dev key lives in `.env`
(copy `.env.example`).

## Decisions already made

All 11 open questions are resolved. Full reasoning, alternatives, costs and breaking points
live in `docs/adr/` — this table is the summary, not the source of truth. **Do not re-decide
any of these without a superseding ADR.**

| # | Decision | ADR |
|---|---|---|
| Pricing shape | A **versioned price list** is the pricing primitive. Plans are lists many customers share; a negotiated deal is a list with one customer on it. No override mechanism — rating has exactly one kind of input. Versions are immutable once referenced. | 0005 |
| Plan change | Fee **and** included allowance prorate by **whole days**. Change day belongs to the new plan. Each segment rated against its own allowance and ladder. | 0006 |
| Rounding | **The customer wins the fraction:** fees round down, allowances round up. Applied once, at the proration boundary. | 0006 |
| Billable request | We authenticated it **and** processed it: `2xx` and client `4xx`. Never `401`/`403`, never our `5xx`, never a limit refusal. Capture happens **after** the outcome is known. | 0007 |
| Limit enforcement | Invert the rupee limit into a **request-count threshold**; the hot path compares two integers. Overshoot budget **~5s**. | 0008 |
| Limit scope | Caps the **total bill including the monthly fee**. A limit below the plan fee is rejected when set. | 0012 |
| Timezone | Store UTC; evaluate boundaries in **Asia/Karachi** (+05:00, no DST). | 0009 |
| Month close | **Reconcile, then issue**, with a bounded grace window as fallback and any shortfall recorded loudly. Late usage **rolls forward** as a labelled prior-period line at its original price version. | 0010 |
| Redis down | **Fail closed** — `503`, no Postgres fallback. Makes the overshoot bound unconditional; costs full availability. | 0011 |
| Corrections | **Credit notes**, never edits. Immutability enforced in the schema now; the mechanism is **not built in v1** and is a stated gap. | 0013 |
| Latency budget | Capture adds **≤1ms at p99**, measured with capture toggled off and on. | 0014 |
| API keys | **Multiple** per customer, stored **hashed**, shown once. Revocation effective **within 30s**. | 0015 |
| Retention | Per-request rows **90 days**; rollups long-term, written at aggregation time. Usage table partitioned so expiry is a partition drop. | 0016 |

### Consequences worth holding in mind

Three of these have sharp edges that will surface in review. They are recorded in the ADRs
and are deliberate, not oversights:

- **The band ladder restarts at each plan-change segment** (0006). A customer whose usage
  straddles an upgrade can pay more than the same usage would have cost on either plan alone.
- **A mid-month upgrade can shrink remaining limit headroom, or exhaust it instantly** (0012),
  because the larger prorated fee consumes more of the same cap — as a direct result of an
  action the customer took expecting more capacity.
- **A Redis restart that returns healthy-but-empty must not serve a single request against a
  zero counter** (0011). Counters are rebuilt from Postgres and marked authoritative before
  traffic is accepted. Rebuild time under load is unmeasured and is the main risk to 0011.

## Decisions are recorded in `docs/adr/`

Every non-obvious choice gets an ADR. Copy `docs/adr/0000-template.md`. An ADR states:
the **decision**, the **alternatives considered**, **what it costs**, and **where it
breaks**. The brief explicitly asks where the design breaks — that is what the "where it
breaks" section is for, and it is not optional.

If you are about to make a choice that a reasonable engineer could have made differently,
write the ADR first. If you are resolving something from `docs/open-questions.md`, the ADR
is how it gets resolved, and the open question is updated to point at it.

## Scope discipline

This repository is built in sessions with hard scope boundaries. Current state:

- **Session 1 (done):** context, agents, ADR process, running skeleton, one echo endpoint
  proving the stack talks to Postgres and Redis. All 11 open questions resolved across
  ADRs 0005-0016, so the schema and the pricing math are both unblocked.
- **Not yet built, deliberately:** billing math, usage recording, the live usage endpoint,
  spending limits, invoicing. No function in this repo calculates money yet.

`GET /v1/echo` touches Postgres and Redis **only to prove connectivity**. It is not a
model for the hot path, and its dependency checks must not survive into a real endpoint.
