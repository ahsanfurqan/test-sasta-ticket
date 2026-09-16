# DESIGN

A paid API where customers pay for what they use. The endpoint is deliberately trivial;
every hard problem here is about counting correctly, charging correctly, and being able to
explain every rupee.

Decisions are recorded as ADRs in [`docs/adr/`](docs/adr/) — 19 of them, each with what it
costs and where it breaks. This document is the map; the ADRs are the reasoning, including
the alternatives that lost. Two ADRs were overturned by building the thing they described,
and both are still in the repo with their corrections attached.

**Status:** all five capabilities work end to end. 232 tests, 4 enforced architecture
contracts. Known gaps are in [`docs/known-gaps.md`](docs/known-gaps.md) and are not hidden
in here.

---

## 1. The conflict the brief actually poses

Three teams asked for things nobody checked against each other:

- Recording usage must not slow the request — **but no request may go unbilled.**
- The usage figure must be current — **but the invoice must be exact and can never change.**
- A spending limit must stop traffic quickly — **without a billing lookup on the hot path.**

Every structural decision below is a ruling on which of those gives. The short version:
**the live figure is allowed to lag and the invoice is not**, and everything else follows.

---

## 2. The pieces, and where the boundaries are

Four deployables — `api`, `worker`, Postgres 16, Redis 7 — from one image, because a
reconciliation job that disagrees with the API about what a charge means is a class of bug
we simply do not have this way.

```
src/meter/
  api/                request serving, auth, capture, limit enforcement, account endpoints
  pipeline/           drain, aggregation, reconciliation, thresholds, month close, invoicing
  segments.py         a billing period -> proration segments        [shared by both]
  billing_calendar.py Asia/Karachi period boundaries (ADR-0009)     [shared by both]
  storage/            ORM models, migrations, repositories
  domain/             PURE pricing math. No I/O, no clock. Ever.
```

Three of those boundaries are **enforced mechanically** by import-linter in `make lint`,
not by discipline:

| Contract | What it prevents |
|---|---|
| `meter.domain` imports no sibling layer | rating reaching for a database mid-calculation |
| `meter.domain` imports nothing that does I/O | a charge that cannot be re-derived from stored inputs |
| `api`/`pipeline` sit above `storage` above `domain` | the layering eroding one convenient import at a time |
| `segments`/`billing_calendar` import neither `api` nor `pipeline` | a second implementation of "which plan, for how many days" |

The last one exists because we hit the problem it prevents. The live usage endpoint and the
invoice run must agree to the paisa, and the only reliable way to guarantee that is one
implementation, not two that look alike.

**The domain purity rule is the load-bearing one.** `meter.domain` is pure functions over
integers: no database handle, no Redis, no clock. Time is an input, never `datetime.now()`.
That is what makes the pricing math property-testable over arbitrary inputs, and what makes
"explain this charge" a re-derivation from stored facts rather than archaeology across three
systems. We verified the contract actually bites by deliberately breaking it and watching
all three fail.

---

## 3. The data model, concretely

Twelve tables. Money is `bigint` paisa in every column — verified there is no `numeric`,
`real`, `double precision` or `money` column anywhere in the schema (ADR-0004).

**Pricing is data, not code** (ADR-0005). A *versioned price list* is the pricing primitive:
a monthly fee, an included quantity, and ordered marginal bands. "Starter", "Growth" and
"Scale" are not special — they are price lists many customers share, and a negotiated deal is
a price list with one customer on it. There is no override mechanism, so rating has exactly
one kind of input.

```
price_lists ── price_list_versions ── price_bands
                       │
customers ── plan_assignments (tstzrange, EXCLUSION constraint)
    ├─ api_keys (hash only, revoked_at, multiple live)
    ├─ billing_periods ── invoices ── invoice_lines
    ├─ spending_limits (limit_paisa + computed request threshold)
    ├─ usage_events   PARTITIONED BY RANGE (billing_period_start)
    └─ usage_rollups  (customer, date, assignment, price list version, api_key)
```

**Invariants pushed into the database, not left to application code**, because the database
is the last line of defence when a bug tries to move a number Finance already sent:

- An **issued invoice and its lines are immutable** by trigger. Attempts to update it, delete
  it, revert it to draft, edit a line, or append a line are all refused — attacked seven ways
  directly in SQL, all seven rejected, each with an error naming the ADR that demands it.
- A **published price list version and its bands are immutable**, so a price change cannot
  reach backwards into a charge already made. A price change is a new version.
- `plan_assignments` carries an **exclusion constraint over its time range**, so a customer
  cannot hold two overlapping plan assignments. That is what makes proration segments
  resolvable at all.
- An invoice cannot be issued with no lines; a version cannot be published with no bands.

`usage_events` is the largest object in the system — hundreds of millions of rows a month at
the design target. It is **range-partitioned by billing period**, with bounds at midnight
Asia/Karachi expressed in UTC, so expiry is a partition drop rather than a mass `DELETE` that
outruns autovacuum on the highest-write table we have. It carries exactly **three indexes** —
primary key, idempotency unique, and `(customer, occurred_at)` — because every index is paid
for on every request.

---

## 4. From a served request to something billable

```
request ─> auth (Redis, 30s cache) ─> limit check (two integers) ─> handler
                                                                      │
                                          XADD to Redis stream  <─────┘
                                                   │
                                          response sent to customer
                                                   │
        worker: XREADGROUP ─> INSERT (idempotency key) ─> commit ─> XACK ─> XTRIM
                                                   │
                                          rollups ─> reconcile ─> invoice
```

**Capture happens after the handler and before the response is sent** (ADR-0018). This is
the single most important ordering decision in the system.

After, because billability depends on the response status: a request is billable if we
authenticated *and processed* it — `2xx` and the customer's own `4xx`. Never a bad key,
never our own `5xx`, never a request refused for hitting a spending limit, because charging
someone to be told "no" is indefensible (ADR-0007).

Before the response, because that is what makes "no request goes unbilled" true rather than
aspirational. **If the process dies before the `XADD`, it also died before the customer
received an answer** — so their retry *is* the request, and nothing is lost. If it dies
after, the usage is already captured. In-process batching is explicitly forbidden: it is the
tempting optimisation, and it would silently reopen exactly this window.

### What happens if that path fails partway

| Failure | What happens | Cost |
|---|---|---|
| API dies between handler and capture | Customer never got an answer; their retry is the request | **Nothing lost** |
| API dies between capture and sending | Usage captured; customer sees a connection error and retries | The retry is a second served request, billed (we did the work twice) |
| Worker dies mid-drain | Batch never `XACK`ed, redelivered, idempotency key dedupes | Nothing lost, nothing double-counted |
| Postgres down | Redis keeps counting, enforcing and buffering; drain resumes | Thresholds go stale; auth serves stale entries (ADR-0019) |
| Postgres down past the stream bound | Fail closed | Full outage, deliberately, rather than serving unrecordable traffic |
| Redis down | `503`, fail closed (ADR-0011) | Full outage; we can neither count nor enforce |
| Redis restarts empty | Counters rebuilt from Postgres and marked authoritative *before* traffic is accepted | **312 ms** for 5,502 customers |
| **Redis process crashes** | AOF fsyncs once a second | **Up to ~1s of usage lost — the honest residual** |

That last row is the limit of the guarantee. It is *"no request is lost to an **application**
crash"*, never the unqualified version. Closing it means `appendfsync always` (latency on
every request) or synchronous Postgres acks (the thing this design exists to avoid).

**Proved, not argued.** 150,000 events with six real `docker compose kill worker` and
committed progress between each: 150,000 rows, 150,000 distinct idempotency keys, zero
duplicates, zero outstanding. The genuinely dangerous window — killed *between* the commit
and the `XACK` — is under a millisecond wide, so it was forced open by hand: on restart the
worker logged `batch of 500: 0 new rows, 500 already present`, four times. And the API
SIGKILL'd under 32 concurrent clients: **162 responses acknowledged, 162 idempotency keys in
the stream, zero missing.**

---

## 5. Fast versus exact

They are the same events read two ways, with different tolerances.

**The live figure** (`GET /v1/usage`) reads the Redis counter and rates it through the same
`meter.segments` → `meter.domain` path the invoice uses. It is near-current, it can lag by
the buffer window, and **the response says so** — a customer who is not told will read it as
a bill.

**The invoice** is rated from `usage_rollups`, which are built from committed
`usage_events`. It is exact, and it is immutable once issued.

They cannot drift apart by anything except the counter's lag, because they are the same
arithmetic over the same events. Reconciliation is what proves it:

```
unexplained = (redis counter − postgres rows) − undrained stream entries
```

That is the number that must be zero. A positive Redis-versus-Postgres gap **on its own is
just healthy lag** — treating that as the alarm would hide a genuine loss behind ordinary
behaviour. `Reconciliation.explain()` prints the SQL alongside the number, always.

**A month closes only when reconciliation proves nothing is outstanding** (ADR-0010), with a
bounded grace window as fallback and any shortfall recorded loudly rather than silently.
Usage arriving after close **rolls forward** onto the next invoice as a labelled prior-period
line, priced at the price list version in effect *when it was incurred*.

We have a live demonstration of this, produced by accident. An operator tool issued a
November invoice while 1.2M requests were still draining. It went out **Rs. 22,049.65 short**,
and the database then refused to let anyone fix it:

```
Nov invoice   31,295,035  (issued short, never touched since)
Dec invoice    3,704,965  including
  "62,999 requests from November 2026, received after that invoice was issued,
   at Rs. 0.35 each (Growth v1)"                              2,204,965

31,295,035 + 3,704,965 − 1,500,000 (Dec's own fee) = 33,500,000 = Rs. 335,000.00
```

That is the brief's worked example, reassembled across an immutable invoice boundary. The
tool that skipped reconciliation has been deleted; `close-customer` (drain → aggregate →
reconcile → issue) is the only path to an invoice.

---

## 6. The spending limit, and how far it can overshoot

The obvious approach — periodically compute what a customer owes and cache it — has overshoot
equal to *the refresh interval times the traffic rate*. **The busier the customer, the further
past their limit they get.** Exactly backwards.

So we ask a different question. The ladder is monotonic in quantity, so a rupee limit inverts
exactly into **the request count at which it is reached**. The hot path compares two integers:

```
if counter >= threshold: refuse
```

No ladder, no price list, no Postgres, no money arithmetic on the request path. The expensive
direction is computed in the background by `pipeline`, which recomputes on every input that
moves it — the limit, the plan, the price list.

The limit caps the **total bill including the monthly fee** (ADR-0012), so a Rs. 50,000 limit
on Growth means Rs. 15,000 of fee and Rs. 35,000 of usage headroom: **threshold 570,000
requests**, at which the bill is exactly Rs. 50,000 and request 570,001 is refused. A limit
below the plan's fee is rejected when it is set, not discovered when traffic stops.

**Longest a customer can go over: the overshoot budget is ~5 seconds**, and its components
are stated rather than hoped for:

- **threshold staleness** — bounded by `threshold_max_age_seconds` (60s) plus the sweep
  interval, published to `meter:threshold:oldest_age_seconds` so it can be alarmed on
  independently of Postgres health;
- **counter lag** — zero by construction, because the counter is incremented in the same
  Redis round trip as the capture, before the response is sent;
- **in-flight concurrency** — requests already in flight when the counter crosses still
  complete. At a few thousand req/s this is a small number of *requests*, not of seconds, and
  it is irreducible without the synchronous check we rejected.

Because we fail closed when Redis is unavailable (ADR-0011), that bound is **unconditional**.
Under any fail-open variant a Redis outage opens an unbounded window in which we serve past a
limit without knowing, and the stated budget quietly becomes a lie.

Demonstrated: threshold 5,012 (a Starter allowance prorated to 15 of 30 days, plus Rs. 10.00
at 80 paisa), 5,200 requests at concurrency 32 → exactly **5,009 served, then refusals**.
Counter stopped at 5,012. All 191 refusals unbilled.

---

## 7. A mid-month plan change, and how we explain it

Commercial handed this back explicitly: our call, as long as we can explain it.

**The fee, the included allowance, and the band widths all prorate by whole days.** The
change day belongs to the new plan. Each segment is rated against its own prorated allowance
and its own prorated ladder. Band *prices* never scale — a price per request has no time
dimension.

> *"You were on Growth for 17 days and Scale for 13. Everything scaled to match — your fee,
> your included requests, and each price tier."*

**Rounding: the customer wins the fraction.** Fees round down, allowances and band bounds
round up. Applied once, at the proration boundary. Across N segments the shortfall is under N
paisa — stated precisely, because the first version of the ADR said "at most a paisa" and
that was wrong.

### The band-width correction

The original decision prorated the fee and the allowance and said nothing about band widths,
accepting as unavoidable that split usage would cost more. Implementing it showed that was
wrong:

| 2,000,000 Growth requests, split 17/13 days | Total | vs unsplit |
|---|---|---|
| No plan change | Rs. 615,000.00 | — |
| Split, band widths **not** prorated | Rs. 689,999.65 | **+Rs. 74,999.65** |
| Split, band widths prorated | Rs. 614,999.80 | −Rs. 0.20 |

Over 99.99% of the penalty came from leaving band widths at full size — revenue collected *by
omission rather than by decision*. Nobody chose to charge a customer Rs. 75,000 for upgrading.

The argument that settles it: **the included allowance is band zero, priced at zero.**
Prorating band zero while leaving bands one and up at full width treats the same kind of
quantity two different ways inside one calculation.

**The residual is honest:** splitting is now 20 paisa *cheaper*, because each segment rounds
its bounds up independently. So "a plan change never changes what you pay for the same usage"
is still not exactly true — only very nearly — and it grows with segment count.

### Explaining any charge

`GET /v1/invoices/{number}` returns every line with its quantity, unit price, amount, and the
**price list version** it was computed from, plus `lines_sum_to_total`. The charge re-derives
from stored facts rather than anyone's memory, and a later price change cannot alter what a
line means.

---

## 8. What we cut, and why that was right

- **Credit notes.** Finance says an issued number never moves, which implies corrections are
  new documents. The *policy* is decided (ADR-0013) and immutability is enforced and proven;
  the mechanism is not built. Correction machinery for invoices that did not yet exist was
  the wrong thing to spend the day on — but it is a real operational gap, not a tidy one.
- **A Django admin.** The brief says no admin screen is needed, so Support explainability was
  built as a deliberate API surface instead. Arguably better; certainly more work.
- **Immediate key revocation.** A 30-second window via cache TTL, rather than a distributed
  invalidation path that must itself be correct under partition. The TTL design is a
  prerequisite for the pub/sub version anyway, so nothing is wasted.
- **Per-customer timezones.** Everything is Asia/Karachi. The customer carries a timezone
  column from the start so the migration is possible, but making it settable is speculative
  until customers are actually elsewhere.
- **Optimising the latency budget.** We are over it and have not fixed it — see below. Fixing
  it by collapsing Redis round trips into a Lua script costs Redis Cluster compatibility, and
  that is a trade worth making deliberately rather than under time pressure.

---

## 9. Where this design struggles as traffic grows

Roughly in the order we would hit them.

**~1,000 req/s, single API container — the latency budget, already missed.** Capture costs
p50 353µs and p99 607µs server-side against ADR-0014's 1ms p99 ceiling; under load at
concurrency 50 the harness measures p99 10.5ms. That p99 is event-loop queueing in a saturated
single-worker container, not Redis — raw Redis from the container is 174–415µs. The real
structural cost is **three Redis round trips per served request**: auth+marker+depth, then
counter+threshold, then capture. Collapsing the first two into one Lua script gets to two
round trips, at the cost of Redis Cluster compatibility, since those keys share no hash tag.

**~5,000 rows/s — the drain, single consumer.** Four consumers in one group split work evenly
and stay exact, so scaling is by adding worker containers. The insert itself does 17,900
rows/s using one `unnest()` array per column; the rest is Redis round trips and parsing.

**Redis memory, before Postgres write throughput.** One `XADD` per request with no batching
means Redis capacity scales with request rate rather than batch count. During a Postgres
outage the stream grows to its bound and then the API fails closed — so **Postgres
availability and Redis memory are coupled in a way neither component's dashboard shows.**

**Hundreds of millions of rows a month — the usage table.** Partitioning makes expiry a
partition drop, and three indexes keep write amplification low. The first thing expected to
outgrow the ORM is bulk insert on this table; it already uses `unnest()` rather than the unit
of work.

**Redis as a single point of everything.** It is in the critical path for availability
(fail-closed), for latency (capture is synchronous), and for enforcement. That is a large bet
on one component, and the honest mitigation is HA and replication, neither of which exists
here.

**Month close, at customer count.** Close is per customer and reconciliation gates the invoice
run. At large customer counts the 1st of the month becomes a scheduling problem rather than a
job.

---

## 10. Evidence

232 tests. What they are chosen to prove matters more than the number.

**Property tests over arbitrary price lists**, including ones nobody would design — band
prices are deliberately *not* forced to decrease, because a property that only holds for
well-behaved input is not a property. Every assertion is exact; a tolerance in a money test
hides the hole it was added for.

- lines sum to the total exactly
- one more request costs exactly one band price — "marginal, not retroactive" as a law
- rating N at once equals rating one at a time across every boundary (the off-by-one detector)
- charged units are exactly those beyond the allowance: none twice, none skipped
- the limit threshold is exact in both directions — one under affordable, one over not
- rounding proved *without dividing*: `fee × days_in_month ≤ monthly_fee × days`, so no float
  appears even in the statement of the property
- `prorate(list, 30, 30) == the original list` — exact object equality

**Failure is injected, not imagined.** Processes actually killed, Redis actually stopped,
Postgres actually stopped, the database actually attacked. `make test-outage` stops Postgres
for real, because a path that runs only during an outage is the least-tested code in the
system and a mock would leave that warning standing.

**The load harness proves a total, not throughput.** 2,000 requests at concurrency 50 →
2,000 served, and 2,000 in the Redis counter, the events table *and* the rollups, converged in
2.1s. It waits for convergence rather than sampling once, because the counter legitimately
leads the rows. **And we verified it can fail:** with the counter tampered +7 against 10
served requests it refuses to converge and names the culprit, while confirming events and
rollups were both right.

---

## 11. Known gaps

In [`docs/known-gaps.md`](docs/known-gaps.md), stated plainly rather than buried here. The
short version: the latency budget is missed and unfixed, credit notes do not exist, a Redis
crash can still lose about a second of usage, and the CLI has no test coverage.
