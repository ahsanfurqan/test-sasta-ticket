# Metered Billing API

A paid API product: customers get an API key, call an endpoint, and pay for how much they
use. Tiered pricing by band, spending limits, and an exact monthly invoice where every
charge can be explained.

**Status: session-1 skeleton.** The stack runs and one endpoint answers. There is no
billing logic yet — no rating, no usage recording, no invoicing — and that is deliberate.
See [CLAUDE.md](CLAUDE.md) for scope, [docs/adr/](docs/adr/) for decisions, and
[docs/open-questions.md](docs/open-questions.md) for what the brief left open.

## Thirty-second quickstart

Docker is the only prerequisite.

```bash
make up
```

That builds the images, starts Postgres 16, Redis, the API and the worker, waits for
health, and applies migrations. Then:

```bash
curl -H "X-API-Key: dev-key-change-me" 'http://localhost:8000/v1/echo?message=hello'
```

```json
{
  "message": "hello",
  "customer_id": "dev-customer",
  "dependencies": { "postgres": "ok", "redis": "ok" },
  "note": "scaffolding only -- this endpoint is not billed and records no usage"
}
```

`"postgres": "ok"` and `"redis": "ok"` are the point of this endpoint: they prove the
container reaches both dependencies. Interactive docs are at
[localhost:8000/docs](http://localhost:8000/docs).

## Commands

| Command | What it does |
|---|---|
| `make up` | Build and start everything, wait for health, run migrations |
| `make down` | Stop and remove containers (`VOLUMES=1` also drops data) |
| `make migrate` | `alembic upgrade head` |
| `make revision m="..."` | Draft a migration (autogenerate — then read and edit it) |
| `make test` | Run the test suite in the api container |
| `make lint` | ruff + import-linter (enforces the `meter.domain` purity seam) |
| `make load-test` | Traffic harness against `/v1/echo` (`N=`, `CONCURRENCY=`) |
| `make logs` / `make ps` | Tail logs / service status |
| `make psql` / `make redis-cli` | Database and Redis shells |

`make help` lists them all.

## Layout

```
src/meter/
  api/         hot-path        auth, serving, usage capture, limit enforcement
  domain/      billing-domain  PURE pricing math — no I/O, ever
  storage/     data-model      ORM models, migrations, repositories
  pipeline/    pipeline        buffering, aggregation, reconciliation, invoicing
  ops/         hot-path        health / readiness
```

`meter.domain` imports nothing from its sibling layers and nothing that performs I/O.
That seam is enforced by import-linter in `make lint`, not by good intentions — it is what
makes the pricing math property-testable and makes explaining a charge a pure re-derivation.

Each directory has one owning agent, defined in [.claude/agents/](.claude/agents/).

## Authentication

`X-API-Key`. Currently one hardcoded key from `.env` (`DEV_API_KEY`) — scaffolding, not a
design. Real per-customer keys, hashing and rotation are open question #10.

## Where to read next

- **[CLAUDE.md](CLAUDE.md)** — the five capabilities, the constraint tensions, the
  money-as-integer rule, ownership.
- **[docs/adr/](docs/adr/)** — every non-obvious decision, including what it costs and
  where it breaks.
- **[docs/open-questions.md](docs/open-questions.md)** — what the brief leaves open, with
  the options and trade-offs. Unresolved on purpose.
- **[DESIGN.md](DESIGN.md)** — the design write-up. A stub for now; the ADRs feed it.
