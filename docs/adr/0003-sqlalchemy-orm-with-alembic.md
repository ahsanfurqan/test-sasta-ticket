# ADR-0003: SQLAlchemy ORM as schema truth, Alembic for migrations

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** data-model

## Context

Choosing FastAPI (ADR-0002) leaves two things unanswered: how schema changes are versioned,
and what executes queries at runtime.

The pull in each direction is real. The usage table is the dominant object in this schema —
at a few thousand requests per second it accumulates on the order of hundreds of millions of
rows per month — and partitioning, index choice and retention on that table are decisions
we must get right the first time, because they are expensive to change under load. That
argues for hand-written DDL. Against it: everything *else* in this schema is ordinary.
Customers, API keys, plans, price lists, spending limits, invoices, invoice lines. A
hand-rolled repository for each is a lot of near-identical code, which is exactly the
duplication DRY exists to prevent.

## Decision

SQLAlchemy 2.0 ORM with declarative models as the source of schema truth, and Alembic for
migrations with autogenerate as the starting point for each revision.

Two qualifications, both deliberate:

- **Autogenerate drafts; a human finishes.** Every generated revision is read and edited
  before it is committed. Autogenerate cannot express declarative partitioning, partial
  indexes, or the immutability constraints we want on issued invoices; those go in by hand
  as `op.execute()` in the same revision.
- **Dropping to Core or raw SQL is allowed where it is measured.** If a specific query on
  the hot path or in the invoice job proves too slow through the ORM, that query drops to
  SQLAlchemy Core or text SQL, with the measurement recorded. Measured, not assumed — see
  "where it breaks".

## Alternatives considered

- **No ORM: hand-written migrations, explicit SQL at runtime.** The original choice here,
  and it has genuine merit for the usage table: `EXPLAIN` on the exact string you execute is
  worth a lot when tuning the largest table in the system. It lost on YAGNI and DRY. We do
  not yet know that ORM overhead is a problem, and building a hand-rolled data-access layer
  for a dozen ordinary tables to avoid a cost nobody has measured is speculative
  optimisation. The escape hatch above keeps the benefit available exactly where it is
  earned.
- **SQLAlchemy Core only, no ORM layer.** Composable SQL without model ceremony. Rejected
  as the worst of both: we still write mapping code by hand, and we lose the migration
  autogeneration that motivated the ORM in the first place.
- **Raw SQL files and a hand-rolled migration runner.** Simpler than Alembic and honest
  about what it is. Rejected because we would rebuild ordering, versioning and a revision
  table, and get them subtly wrong.

## What it costs

- **Autogenerate lulls you.** A generated migration that applies cleanly on an empty laptop
  database can be an outage on a large table. Every revision states what it locks and for
  how long at production scale — that is why the migration template has a `Locking:` field.
- **Lazy loading is a latency trap.** An attribute access that silently issues a query is
  invisible in a unit test and fatal on a request path. Relationship loading is explicit;
  `expire_on_commit=False` on the session factory stops a commit from re-querying.
- **The ORM makes N+1 easy to write and hard to see.** This is a real ongoing tax on review,
  not a one-off cost.
- **Async ORM needs `greenlet`**, and async sessions are not safe to share across tasks.
  One session per unit of work, never a module-level global.

## Where it breaks

The ORM stops being the right answer for the **usage table specifically**, and probably
sooner than for anything else here. Once that table is partitioned and carrying hundreds of
millions of rows a month, bulk insert through the ORM's unit of work is the wrong shape —
the write path wants `COPY` or a multi-row insert, not per-object flushes. Expect that path
to drop to Core or raw SQL, and expect it to be the first thing that does.

It also breaks if "drop to raw SQL where measured" becomes "drop to raw SQL where
convenient". The discipline is the measurement, not the escape hatch, and the signal that
this has failed is raw SQL appearing with no benchmark attached.

Finally, note the sharp edge this creates: the ORM makes it trivially easy to run a query
from anywhere, including from a request handler. `meter.domain` is protected mechanically
(import-linter forbids it importing `sqlalchemy` at all), but the hot path is protected only
by review.

## Consequences for other owners

`data-model` owns the models and reads every generated migration before committing it.
`billing-domain` is unaffected: `meter.domain` imports no SQLAlchemy and no I/O of any kind,
enforced by `make lint`. `hot-path` and `pipeline` consume repositories from `meter.storage`
rather than issuing queries inline, and `hot-path` keeps its standing question — what did
this add to p99? — pointed at ORM calls in particular.
