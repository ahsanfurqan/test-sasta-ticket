"""Usage: the drain's insert, the rollup upsert, and the counts reconciliation proves with.

Two statements in here carry most of the system's correctness:

1. ``INSERT ... ON CONFLICT (idempotency_key, billing_period_start) DO NOTHING`` -- the one
   thing standing between a redelivered batch and a double-counted request (ADR-0018).
2. The rollup upsert, which **assigns** the aggregated counts rather than adding to them.
   Adding would make the pass non-idempotent, and a pass that cannot safely be re-run is a
   pass that loses a day's rollup the first time the worker is killed. Assigning means
   re-aggregating a day is a no-op, which is what makes the whole pipeline replayable.

The rollup is deliberately NOT guarded with ``WHERE invoice_id IS NULL``. Late usage for an
already-invoiced period must still reach the rollup -- the rollup is the current truth of
what happened, the invoice is the immutable record of what was billed, and ADR-0010's
roll-forward is exactly the difference between them. Silently refusing the update would
discard the late usage, which is the one outcome ADR-0010 rules out.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

BILLING_TZ = "Asia/Karachi"  # ADR-0009


@dataclass(frozen=True, slots=True)
class Cell:
    """One (customer, local day) rollup cell, with its UTC bounds already resolved.

    The bounds arrive resolved because `meter.storage` must not know about timezones
    beyond the SQL above -- the conversion happens once, in `meter.pipeline.clock`.
    """

    customer_id: str
    usage_date: date
    period_start: datetime
    day_start: datetime
    day_end: datetime


@dataclass(frozen=True, slots=True)
class SegmentUsage:
    """Billable usage for one plan segment of one period -- what a charge is rated from."""

    plan_assignment_id: str
    price_list_version_id: str
    billable_requests: int


_USAGE_COLUMNS = (
    "billing_period_start",
    "customer_id",
    "api_key_id",
    "occurred_at",
    "status_code",
    "outcome",
    "billable",
    "idempotency_key",
)

# EIGHT bind parameters, whatever the batch size: one array per column, unnested into rows
# by Postgres. Measured against the obvious alternative -- a multi-row VALUES list with
# eight parameters per row -- on this machine:
#
#     batch    VALUES list      unnest
#       500     8,500 rows/s   12,200 rows/s
#     2,000     5,500 rows/s   17,900 rows/s
#     4,000     2,800 rows/s   16,700 rows/s
#
# The VALUES form gets *worse* as the batch grows, because compiling a statement with
# 32,000 named parameters is superlinear in Python, and it hits asyncpg's hard ceiling of
# 32,767 parameters per statement at 4,095 rows. The array form compiles once and is
# cached by SQLAlchemy, so batch size becomes a tuning knob rather than a cliff.
#
# `RETURNING 1` survives the change, which matters: with ON CONFLICT DO NOTHING it returns
# a row only for what was actually inserted, and that count is the deduplication evidence
# the crash-safety tests assert on.
INSERT_USAGE_SQL = text(
    """
INSERT INTO usage_events (
    billing_period_start, customer_id, api_key_id, occurred_at,
    status_code, outcome, billable, idempotency_key
)
SELECT * FROM unnest(
    CAST(:billing_period_start AS timestamptz[]),
    CAST(CAST(:customer_id AS text[]) AS uuid[]),
    CAST(CAST(:api_key_id AS text[]) AS uuid[]),
    CAST(:occurred_at AS timestamptz[]),
    CAST(:status_code AS smallint[]),
    CAST(CAST(:outcome AS text[]) AS usage_outcome[]),
    CAST(:billable AS boolean[]),
    CAST(:idempotency_key AS text[])
)
ON CONFLICT (idempotency_key, billing_period_start) DO NOTHING
RETURNING 1
"""
)


async def insert_usage_events(conn: AsyncConnection, rows: list[dict]) -> int:
    """Commit a drained batch in ONE statement. Returns how many rows were genuinely new.

    The gap between ``len(rows)`` and the return value IS the deduplication: on a
    redelivered batch it is the whole batch, and that count is what the crash-safety test
    asserts against rather than inferring it from totals.

    One statement, not one per row, for two reasons: a batch of round trips would be the
    drain's throughput ceiling, and a single statement is a single atomic unit, so
    "committed" and "not committed" are the only two states a crash can leave behind.
    """
    if not rows:
        return 0

    # Intra-batch duplicates: the hot path mints one key per event, so two entries with
    # the same key are a redelivery that happened to land in one batch. Collapse them here
    # rather than relying on ON CONFLICT to arbitrate a row against itself.
    unique: dict[tuple[str, object], dict] = {}
    for row in rows:
        unique.setdefault((row["idempotency_key"], row["billing_period_start"]), row)
    deduplicated = list(unique.values())

    columns = {
        column: [row[column] for row in deduplicated] for column in _USAGE_COLUMNS
    }
    result = await conn.execute(INSERT_USAGE_SQL, columns)
    return len(result.fetchall())


# ---------------------------------------------------------------------------------------
# Aggregation (ADR-0016). Grain: customer x local day x price list version x segment x key.
# ---------------------------------------------------------------------------------------

_AGGREGATE_SQL = """
INSERT INTO usage_rollups (
    customer_id, billing_period_id, usage_date, price_list_version_id,
    plan_assignment_id, api_key_id, billable_requests, non_billable_requests
)
SELECT e.customer_id,
       bp.id,
       (e.occurred_at AT TIME ZONE '{tz}')::date,
       pa.price_list_version_id,
       pa.id,
       e.api_key_id,
       count(*) FILTER (WHERE e.billable),
       count(*) FILTER (WHERE NOT e.billable)
  FROM usage_events e
  -- The exclusion constraint on plan_assignments guarantees at most one match, so this
  -- join cannot fan out. A row with NO match is a request from a customer who was on no
  -- plan at that instant: it is excluded here and counted by unattributed_events(), never
  -- silently forgotten.
  JOIN plan_assignments pa
    ON pa.customer_id = e.customer_id
   AND pa.effective @> e.occurred_at
  JOIN billing_periods bp
    ON bp.customer_id = e.customer_id
   AND bp.period_start = e.billing_period_start
  -- usage_events carries NO foreign keys, by design (ADR-0016: an FK is a per-row index
  -- probe on the hottest write path). usage_rollups does. So a usage row naming a key that
  -- is not in api_keys would fail this INSERT on fk_usage_rollups_api_key -- and, because
  -- a pass aggregates many cells in one statement, one such row would block every other
  -- customer's rollup for as long as it existed. Joining makes the constraint satisfiable
  -- by construction and turns a wedged pipeline into a counted anomaly.
  JOIN api_keys ak
    ON ak.id = e.api_key_id
 WHERE {predicate}
 GROUP BY 1, 2, 3, 4, 5, 6
ON CONFLICT (customer_id, usage_date, plan_assignment_id, price_list_version_id, api_key_id)
DO UPDATE SET billable_requests     = EXCLUDED.billable_requests,
              non_billable_requests = EXCLUDED.non_billable_requests
"""


async def aggregate_cells(conn: AsyncConnection, cells: list[Cell]) -> int:
    """Re-aggregate specific (customer, local day) cells from scratch.

    Every predicate names ``billing_period_start`` so the planner prunes to one partition,
    and ``customer_id`` + ``occurred_at`` so it uses ix_usage_events_customer_occurred.
    """
    if not cells:
        return 0
    clauses = []
    params: dict[str, object] = {}
    for index, cell in enumerate(cells):
        clauses.append(
            f"(e.billing_period_start = :ps{index} AND e.customer_id = :cu{index} "
            f"AND e.occurred_at >= :ds{index} AND e.occurred_at < :de{index})"
        )
        params[f"ps{index}"] = cell.period_start
        params[f"cu{index}"] = cell.customer_id
        params[f"ds{index}"] = cell.day_start
        params[f"de{index}"] = cell.day_end
    sql = _AGGREGATE_SQL.format(tz=BILLING_TZ, predicate=" OR ".join(clauses))
    result = await conn.execute(text(sql), params)
    return result.rowcount or 0


async def aggregate_period(
    conn: AsyncConnection, customer_id: str, period_start: datetime
) -> int:
    """Re-aggregate a whole customer-period from the per-request rows.

    This is the safety net behind the incremental pass. The incremental pass tracks dirty
    cells in Redis, and Redis is not the system of record (invariant 3) -- so month close
    re-aggregates the entire period from Postgres before it reconciles, and a lost dirty
    set costs a little work rather than a wrong invoice.
    """
    sql = _AGGREGATE_SQL.format(
        tz=BILLING_TZ,
        predicate="e.billing_period_start = :period_start AND e.customer_id = :customer_id",
    )
    result = await conn.execute(
        text(sql), {"period_start": period_start, "customer_id": customer_id}
    )
    return result.rowcount or 0


# ---------------------------------------------------------------------------------------
# The reconciliation queries. These are quoted verbatim in the reconciliation report, so
# the number it produces always arrives with the query that produced it.
# ---------------------------------------------------------------------------------------

EVENTS_BILLABLE_SQL = """
SELECT count(*) AS billable
  FROM usage_events
 WHERE customer_id = :customer_id
   AND billing_period_start = :period_start
   AND billable
"""

ROLLUPS_BILLABLE_SQL = """
SELECT COALESCE(sum(billable_requests), 0) AS billable
  FROM usage_rollups
 WHERE customer_id = :customer_id
   AND billing_period_id = :period_id
"""

UNATTRIBUTED_SQL = """
SELECT count(*) AS unattributed
  FROM usage_events e
 WHERE e.customer_id = :customer_id
   AND e.billing_period_start = :period_start
   AND (
       NOT EXISTS (
           SELECT 1 FROM plan_assignments pa
            WHERE pa.customer_id = e.customer_id
              AND pa.effective @> e.occurred_at
       )
       OR NOT EXISTS (SELECT 1 FROM api_keys ak WHERE ak.id = e.api_key_id)
   )
"""


async def events_billable(
    conn: AsyncConnection, customer_id: str, period_start: datetime
) -> int:
    """Postgres truth at the per-request grain. The number Redis is measured against."""
    return int(
        await conn.scalar(
            text(EVENTS_BILLABLE_SQL),
            {"customer_id": customer_id, "period_start": period_start},
        )
    )


async def rollups_billable(conn: AsyncConnection, customer_id: str, period_id: str) -> int:
    """Postgres truth at the rollup grain -- the number that survives the 90-day expiry."""
    return int(
        await conn.scalar(
            text(ROLLUPS_BILLABLE_SQL),
            {"customer_id": customer_id, "period_id": period_id},
        )
    )


async def unattributed_events(
    conn: AsyncConnection, customer_id: str, period_start: datetime
) -> int:
    """Requests that reached `usage_events` but cannot reach a rollup.

    Two causes, both real usage: the customer was on no plan at that instant (so there is
    no price list version to name), or the row names an API key that does not exist (which
    the usage table cannot prevent, because it deliberately carries no foreign keys).

    Either way the request is recorded and unbillable. Reconciliation reports it separately
    instead of letting it look like drain lag, because lag clears itself in seconds and
    this never does.
    """
    return int(
        await conn.scalar(
            text(UNATTRIBUTED_SQL),
            {"customer_id": customer_id, "period_start": period_start},
        )
    )


async def billable_counts_by_customer(
    conn: AsyncConnection, period_start: datetime
) -> dict[str, int]:
    """Every customer's billable count for one period, in one pass.

    This is the counter rebuild of ADR-0011: one grouped scan of one partition, not a
    query per customer. How long it takes is the number ADR-0011 says is missing.
    """
    rows = (
        await conn.execute(
            text(
                "SELECT customer_id, count(*) AS billable "
                "  FROM usage_events "
                " WHERE billing_period_start = :period_start AND billable "
                " GROUP BY customer_id"
            ),
            {"period_start": period_start},
        )
    ).all()
    return {str(row.customer_id): int(row.billable) for row in rows}


async def segment_usage(
    conn: AsyncConnection, customer_id: str, period_id: str
) -> list[SegmentUsage]:
    """Billable usage per plan segment for a period -- the input to `rate_period`."""
    rows = (
        await conn.execute(
            text(
                "SELECT plan_assignment_id, price_list_version_id, "
                "       COALESCE(sum(billable_requests), 0) AS billable "
                "  FROM usage_rollups "
                " WHERE customer_id = :customer_id AND billing_period_id = :period_id "
                " GROUP BY plan_assignment_id, price_list_version_id"
            ),
            {"customer_id": customer_id, "period_id": period_id},
        )
    ).all()
    return [
        SegmentUsage(
            plan_assignment_id=str(row.plan_assignment_id),
            price_list_version_id=str(row.price_list_version_id),
            billable_requests=int(row.billable),
        )
        for row in rows
    ]


async def stamp_invoice(conn: AsyncConnection, period_id: str, invoice_id: str) -> int:
    """Mark a period's rollups as having reached an invoice.

    Only rows that have never been billed are stamped. A rollup that grew after its
    invoice was issued keeps its original stamp, so `ix_usage_rollups_unbilled` stays the
    "never billed at all" index and roll-forward is computed from the invoice lines, which
    are immutable, rather than from a mutable flag.
    """
    result = await conn.execute(
        text(
            "UPDATE usage_rollups SET invoice_id = :invoice_id "
            " WHERE billing_period_id = :period_id AND invoice_id IS NULL"
        ),
        {"invoice_id": invoice_id, "period_id": period_id},
    )
    return result.rowcount or 0
