"""The drain, and the proof that killing it mid-flight loses and duplicates nothing.

ADR-0018 makes a specific promise: a worker that dies mid-drain leaves its batch pending,
the batch is redelivered, and the idempotency key makes redelivery safe. These tests prove
it by *injection* rather than by argument, which is what the ADR asks for.

The injection is exact rather than approximate. A `SIGKILL` between the Postgres commit and
the `XACK` leaves precisely one observable state behind: the rows are in Postgres and the
entries are still in the consumer group's pending list under the dead consumer's name.
These tests produce that state directly -- `XREADGROUP` under the consumer name, then
commit, then simply never ack -- and then start a fresh `Drain` with the same name, which
is what the restarted container does. No sleeps, no timing, no flakiness, and the same
state a real kill produces. The real `docker compose kill worker` run is in the session
report; this is the version that runs on every CI pass.

This module also owns the shared seeding helpers, imported by the other two integration
modules in this session. They would belong in `tests/conftest.py`, which is `test-engineer`'s
file and not this agent's to edit.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meter import billing_calendar as clock
from meter.config import Settings, get_settings
from meter.domain.catalogue import GROWTH_V1
from meter.domain.plans import PriceList
from meter.pipeline import keys
from meter.pipeline.drain import Drain
from meter.pipeline.events import UsageEventRecord, encode
from meter.storage import cache, db
from meter.storage.repositories import periods, rollups

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    created = db.create_engine(get_settings())
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture
async def redis() -> AsyncIterator[aioredis.Redis]:
    client = cache.create_client(get_settings())
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def settings() -> Settings:
    """A private stream per test.

    The worker container is running and draining `usage:events` for real. A test that
    shared the stream would race a live consumer for its own entries, and the failure
    would look like a drain bug rather than a test bug.
    """
    base = get_settings().model_dump()
    base["usage_stream_key"] = f"test:usage:{uuid.uuid4().hex[:12]}"
    base["usage_stream_group"] = "drain"
    base["drain_batch_size"] = 100
    base["drain_block_ms"] = 50
    base["drain_reclaim_idle_ms"] = 30_000
    base["drain_consumer_name"] = "worker-1"
    return Settings(**base)


@pytest.fixture
async def drain(settings, engine, redis) -> AsyncIterator[Drain]:
    drainer = Drain(settings, engine, redis)
    await drainer.ensure_group()
    try:
        yield drainer
    finally:
        await redis.delete(
            settings.usage_stream_key, keys.dead_letter_stream(settings.usage_stream_key)
        )


# ---------------------------------------------------------------------------------------
# Seeding. Committed, not rolled back: the drain runs in its own engine and its own
# transaction, so a test that held everything open in one would deadlock against itself.
# ---------------------------------------------------------------------------------------


async def seed_customer(engine: AsyncEngine, name: str = "Acme") -> tuple[str, str]:
    """A customer and one API key. Returns (customer_id, api_key_id)."""
    async with engine.begin() as conn:
        customer_id = await conn.scalar(
            text("INSERT INTO customers (name) VALUES (:name) RETURNING id"),
            {"name": f"{name} {uuid.uuid4().hex[:8]}"},
        )
        api_key_id = await conn.scalar(
            text(
                "INSERT INTO api_keys (customer_id, key_hash, prefix) "
                "VALUES (:customer_id, :key_hash, 'mk_test') RETURNING id"
            ),
            {
                "customer_id": customer_id,
                "key_hash": f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
            },
        )
    return str(customer_id), str(api_key_id)


async def seed_price_list(engine: AsyncEngine, price_list: PriceList = GROWTH_V1) -> str:
    """One published price list version, seeded from the launch catalogue.

    The catalogue is seed data, not the runtime source of truth (ADR-0005) -- which is
    exactly what it is being used as here.
    """
    async with engine.begin() as conn:
        list_id = await conn.scalar(
            text("INSERT INTO price_lists (name) VALUES (:name) RETURNING id"),
            {"name": f"{price_list.name}-{uuid.uuid4().hex[:8]}"},
        )
        version_id = await conn.scalar(
            text(
                "INSERT INTO price_list_versions "
                "  (price_list_id, version, name, monthly_fee_paisa, included_quantity) "
                "VALUES (:list_id, :version, :name, :fee, :included) RETURNING id"
            ),
            {
                "list_id": list_id,
                "version": price_list.version,
                "name": price_list.name,
                "fee": price_list.monthly_fee_paisa,
                "included": price_list.included_quantity,
            },
        )
        for index, band in enumerate(price_list.bands):
            await conn.execute(
                text(
                    "INSERT INTO price_bands "
                    "  (price_list_version_id, band_index, up_to, unit_price_paisa) "
                    "VALUES (:version_id, :index, :up_to, :price)"
                ),
                {
                    "version_id": version_id,
                    "index": index,
                    "up_to": band.up_to,
                    "price": band.unit_price_paisa,
                },
            )
        await conn.execute(
            text("UPDATE price_list_versions SET published_at = now() WHERE id = :id"),
            {"id": version_id},
        )
    return str(version_id)


async def seed_assignment(
    engine: AsyncEngine,
    customer_id: str,
    version_id: str,
    start: datetime,
    end: datetime | None = None,
) -> str:
    async with engine.begin() as conn:
        return str(
            await conn.scalar(
                text(
                    "INSERT INTO plan_assignments "
                    "  (customer_id, price_list_version_id, effective) "
                    "VALUES (:customer_id, :version_id, tstzrange(:start, :end, '[)')) "
                    "RETURNING id"
                ),
                {
                    "customer_id": customer_id,
                    "version_id": version_id,
                    "start": start,
                    "end": end,
                },
            )
        )


async def seed_partition(engine: AsyncEngine, month) -> None:
    async with engine.begin() as conn:
        await periods.ensure_usage_partition(conn, month)


async def seed_limit(
    engine: AsyncEngine, customer_id: str, period_id: str, limit_paisa: int
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO spending_limits (customer_id, billing_period_id, limit_paisa) "
                "VALUES (:customer_id, :period_id, :limit) "
                "ON CONFLICT (customer_id, billing_period_id) "
                "DO UPDATE SET limit_paisa = EXCLUDED.limit_paisa"
            ),
            {"customer_id": customer_id, "period_id": period_id, "limit": limit_paisa},
        )


def make_record(
    customer_id: str,
    api_key_id: str,
    occurred_at: datetime,
    *,
    key: str | None = None,
    billable: bool = True,
    status_code: int = 200,
    outcome: str = "success",
) -> UsageEventRecord:
    return UsageEventRecord(
        idempotency_key=key or uuid.uuid4().hex,
        customer_id=customer_id,
        api_key_id=api_key_id,
        occurred_at=occurred_at,
        billing_period_start=clock.period_start_of(occurred_at),
        status_code=status_code,
        outcome=outcome,
        billable=billable,
    )


def burst(
    customer_id: str,
    api_key_id: str,
    start: datetime,
    count: int,
    **kwargs,
) -> list[UsageEventRecord]:
    """`count` requests a second apart, as the hot path would have captured them."""
    return [
        make_record(customer_id, api_key_id, start + timedelta(seconds=n), **kwargs)
        for n in range(count)
    ]


async def publish(redis: aioredis.Redis, stream: str, records: list[UsageEventRecord]) -> None:
    """Exactly what the hot path's middleware does: one XADD per request, no batching."""
    pipe = redis.pipeline(transaction=False)
    for record in records:
        pipe.xadd(stream, encode(record))
    await pipe.execute()


async def count_events(engine: AsyncEngine, customer_id: str, period_start) -> int:
    async with engine.connect() as conn:
        return await conn.scalar(
            text(
                "SELECT count(*) FROM usage_events "
                " WHERE customer_id = :customer_id AND billing_period_start = :period_start"
            ),
            {"customer_id": customer_id, "period_start": period_start},
        )


async def count_distinct_keys(engine: AsyncEngine, customer_id: str, period_start) -> int:
    async with engine.connect() as conn:
        return await conn.scalar(
            text(
                "SELECT count(DISTINCT idempotency_key) FROM usage_events "
                " WHERE customer_id = :customer_id AND billing_period_start = :period_start"
            ),
            {"customer_id": customer_id, "period_start": period_start},
        )


# A month with no live traffic in it, so a test's counts are its own.
TEST_MONTH_START = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------------------


async def test_a_batch_is_committed_then_acked(settings, engine, redis, drain):
    customer_id, api_key_id = await seed_customer(engine)
    await seed_partition(engine, clock.period_month(TEST_MONTH_START))
    records = burst(customer_id, api_key_id, TEST_MONTH_START, 25)
    await publish(redis, settings.usage_stream_key, records)

    result = await drain.run_once(block_ms=0)

    assert result.entries_read == 25
    assert result.rows_inserted == 25
    assert result.duplicates_ignored == 0
    assert result.acked == 25
    assert await drain.outstanding() == 0
    assert await count_events(engine, customer_id, clock.period_start_of(TEST_MONTH_START)) == 25


async def test_the_drain_reports_its_lag(settings, engine, redis, drain):
    """Invariant 7: "how far behind is the live figure?" must be a number, not a guess."""
    customer_id, api_key_id = await seed_customer(engine)
    await seed_partition(engine, clock.period_month(clock.now()))
    occurred = clock.now() - timedelta(seconds=3)
    await publish(
        redis, settings.usage_stream_key, [make_record(customer_id, api_key_id, occurred)]
    )

    result = await drain.run_once(block_ms=0)

    assert result.lag_ms is not None
    assert 2_500 <= result.lag_ms <= 60_000
    assert int(await redis.get(keys.DRAIN_LAG_MS)) == result.lag_ms


# ---------------------------------------------------------------------------------------
# Crash safety. Each test reproduces one row of ADR-0018's failure matrix exactly.
# ---------------------------------------------------------------------------------------


async def _deliver_without_acking(
    redis: aioredis.Redis, settings: Settings, count: int, *, new: bool = True
) -> list[str]:
    """Put entries into the pending list under the worker's consumer name and leave them.

    This IS the state a SIGKILL leaves behind. Redis has already recorded the delivery;
    the process that received it is gone.
    """
    response = await redis.xreadgroup(
        groupname=settings.usage_stream_group,
        consumername=settings.drain_consumer_name,
        # ">" is new deliveries; "0" re-reads what is already pending for this consumer,
        # which is what a restarted process sees. No BLOCK: BLOCK 0 waits forever.
        streams={settings.usage_stream_key: ">" if new else "0"},
        count=count,
    )
    return [entry_id for _stream, entries in response for entry_id, _fields in entries]


async def test_killed_before_the_commit_loses_nothing(settings, engine, redis, drain):
    """Matrix row: "worker dies mid-drain", killed BEFORE the Postgres commit.

    Nothing is in Postgres and nothing is acked, so the entries are still pending. A
    restarted worker with the same consumer name picks them up and inserts every one.
    """
    customer_id, api_key_id = await seed_customer(engine)
    period_start = clock.period_start_of(TEST_MONTH_START)
    await seed_partition(engine, clock.period_month(TEST_MONTH_START))
    records = burst(customer_id, api_key_id, TEST_MONTH_START, 40)
    await publish(redis, settings.usage_stream_key, records)

    delivered = await _deliver_without_acking(redis, settings, 40)
    assert len(delivered) == 40
    assert await count_events(engine, customer_id, period_start) == 0
    assert await drain.outstanding() == 40

    restarted = Drain(settings, engine, redis)  # same consumer name: same container, restarted
    result = await restarted.run_once(block_ms=0)

    assert result.entries_read == 40
    assert result.rows_inserted == 40, "every request must reach Postgres"
    assert await count_events(engine, customer_id, period_start) == 40
    assert await restarted.outstanding() == 0


async def test_killed_between_the_commit_and_the_ack_double_counts_nothing(
    settings, engine, redis, drain
):
    """Matrix row: "worker dies mid-drain", killed AFTER the commit, BEFORE the XACK.

    This is the dangerous half: the rows ARE durable and Redis still believes the batch is
    outstanding. Redelivery is unavoidable; the idempotency key is what makes it harmless.
    """
    customer_id, api_key_id = await seed_customer(engine)
    period_start = clock.period_start_of(TEST_MONTH_START)
    await seed_partition(engine, clock.period_month(TEST_MONTH_START))
    records = burst(customer_id, api_key_id, TEST_MONTH_START, 40)
    await publish(redis, settings.usage_stream_key, records)

    await _deliver_without_acking(redis, settings, 40)
    async with engine.begin() as conn:
        committed = await rollups.insert_usage_events(
            conn, [record.as_row() for record in records]
        )
    assert committed == 40
    # ... and here the process dies, with the XACK never sent.

    restarted = Drain(settings, engine, redis)
    result = await restarted.run_once(block_ms=0)

    assert result.entries_read == 40, "the batch must be redelivered"
    assert result.rows_inserted == 0, "not one of them may be inserted a second time"
    assert result.duplicates_ignored == 40
    assert result.acked == 40, "and this time it is acked"

    assert await count_events(engine, customer_id, period_start) == 40
    assert await count_distinct_keys(engine, customer_id, period_start) == 40
    assert await restarted.outstanding() == 0


async def test_redelivery_five_times_over_still_counts_each_request_once(
    settings, engine, redis, drain
):
    """The pathological case: a worker that keeps dying at the worst moment.

    Five crashes between commit and ack. The count after is the count after one clean run,
    which is the whole claim of ADR-0018 section 3 stated as a number.
    """
    customer_id, api_key_id = await seed_customer(engine)
    period_start = clock.period_start_of(TEST_MONTH_START)
    await seed_partition(engine, clock.period_month(TEST_MONTH_START))
    records = burst(customer_id, api_key_id, TEST_MONTH_START, 30)
    await publish(redis, settings.usage_stream_key, records)

    delivered = await _deliver_without_acking(redis, settings, 30)
    assert len(delivered) == 30
    for _ in range(5):
        # Each cycle: the restarted worker re-reads its own pending batch, commits it, and
        # is killed again before the XACK.
        redelivered = await _deliver_without_acking(redis, settings, 30, new=False)
        assert len(redelivered) == 30
        async with engine.begin() as conn:
            await rollups.insert_usage_events(conn, [record.as_row() for record in records])

    final = Drain(settings, engine, redis)
    result = await final.run_once(block_ms=0)

    assert result.rows_inserted == 0
    assert await count_events(engine, customer_id, period_start) == 30
    assert await final.outstanding() == 0


async def test_a_batch_abandoned_by_a_dead_consumer_is_reclaimed(
    settings, engine, redis, drain
):
    """Matrix row: killed and never restarted, or restarted under a different name.

    `XAUTOCLAIM` moves the batch to a live consumer once it has been idle long enough.
    Redelivery to a DIFFERENT consumer is safe for the same reason as to the same one.
    """
    customer_id, api_key_id = await seed_customer(engine)
    period_start = clock.period_start_of(TEST_MONTH_START)
    await seed_partition(engine, clock.period_month(TEST_MONTH_START))
    records = burst(customer_id, api_key_id, TEST_MONTH_START, 12)
    await publish(redis, settings.usage_stream_key, records)
    await _deliver_without_acking(redis, settings, 12)

    survivor_settings = Settings(
        **{
            **settings.model_dump(),
            "drain_consumer_name": "worker-2",
            "drain_reclaim_idle_ms": 0,
        }
    )
    survivor = Drain(survivor_settings, engine, redis)
    result = await survivor.run_once(block_ms=0)

    assert result.entries_read == 12
    assert result.rows_inserted == 12
    assert await count_events(engine, customer_id, period_start) == 12
    assert await survivor.outstanding() == 0


async def test_a_partly_redelivered_batch_inserts_only_what_is_missing(
    settings, engine, redis, drain
):
    """A crash part-way through is not a special case, because the insert is one statement
    -- but a batch can still overlap rows an earlier batch committed. The dedup is
    per-row, so the arithmetic works out whatever the overlap is."""
    customer_id, api_key_id = await seed_customer(engine)
    period_start = clock.period_start_of(TEST_MONTH_START)
    await seed_partition(engine, clock.period_month(TEST_MONTH_START))
    records = burst(customer_id, api_key_id, TEST_MONTH_START, 20)
    await publish(redis, settings.usage_stream_key, records)
    await _deliver_without_acking(redis, settings, 20)
    async with engine.begin() as conn:
        await rollups.insert_usage_events(
            conn, [record.as_row() for record in records[:7]]  # a partial commit's worth
        )

    restarted = Drain(settings, engine, redis)
    result = await restarted.run_once(block_ms=0)

    assert result.entries_read == 20
    assert result.rows_inserted == 13
    assert result.duplicates_ignored == 7
    assert await count_events(engine, customer_id, period_start) == 20


# ---------------------------------------------------------------------------------------
# Entries that cannot become rows
# ---------------------------------------------------------------------------------------


async def test_a_malformed_entry_is_dead_lettered_never_dropped(settings, engine, redis, drain):
    customer_id, api_key_id = await seed_customer(engine)
    await seed_partition(engine, clock.period_month(TEST_MONTH_START))
    good = make_record(customer_id, api_key_id, TEST_MONTH_START)
    await publish(redis, settings.usage_stream_key, [good])
    await redis.xadd(
        settings.usage_stream_key,
        {"idempotency_key": "short", "customer_id": customer_id, "outcome": "success"},
    )

    result = await drain.run_once(block_ms=0)

    assert result.entries_read == 2
    assert result.rows_inserted == 1
    assert result.dead_lettered == 1
    assert result.acked == 2, "a poison entry must not block the batch behind it forever"

    dead = await redis.xrange(keys.dead_letter_stream(settings.usage_stream_key))
    assert len(dead) == 1
    assert "_reason" in dead[0][1]


async def test_a_naive_timestamp_is_refused_rather_than_guessed(settings, engine, redis, drain):
    """Asia/Karachi is +05:00. A timestamp without an offset is ambiguous by five hours,
    which at a month boundary is a whole day's usage in the wrong period."""
    customer_id, api_key_id = await seed_customer(engine)
    await redis.xadd(
        settings.usage_stream_key,
        {
            "idempotency_key": uuid.uuid4().hex,
            "customer_id": customer_id,
            "api_key_id": api_key_id,
            "occurred_at": "2026-03-10T09:00:00",
            "status_code": "200",
            "outcome": "success",
            "billable": "1",
        },
    )

    result = await drain.run_once(block_ms=0)

    assert result.dead_lettered == 1
    assert result.rows_inserted == 0


# ---------------------------------------------------------------------------------------
# Group creation
# ---------------------------------------------------------------------------------------


async def test_the_group_starts_at_the_beginning_of_the_stream(settings, engine, redis):
    """Entries captured before the drain first ran are still billable requests.

    Creating the group at `$` would skip them, silently, and the only evidence would be an
    invoice that is short.
    """
    customer_id, api_key_id = await seed_customer(engine)
    await seed_partition(engine, clock.period_month(TEST_MONTH_START))
    records = burst(customer_id, api_key_id, TEST_MONTH_START, 5)
    await publish(redis, settings.usage_stream_key, records)

    drainer = Drain(settings, engine, redis)
    await drainer.ensure_group()  # first time the group has ever existed
    result = await drainer.run_once(block_ms=0)

    assert result.rows_inserted == 5
    await redis.delete(settings.usage_stream_key)


async def test_ensure_group_is_idempotent(settings, engine, redis, drain):
    await drain.ensure_group()
    await drain.ensure_group()
    assert await drain.outstanding() == 0


# ---------------------------------------------------------------------------------------
# The stream bound (ADR-0018)
# ---------------------------------------------------------------------------------------


async def test_drained_entries_are_trimmed_and_pending_ones_are_not(
    settings, engine, redis, drain
):
    """`XACK` clears the pending list; it does NOT shorten the stream.

    Nothing else trims, so without this the stream grows by one entry per request forever
    and walks into `usage_stream_max_entries` -- at which point the hot path fails closed
    (ADR-0018). A Postgres-outage safety valve tripping on ordinary success is a worse
    outage than the one it was built to contain.
    """
    customer_id, api_key_id = await seed_customer(engine)
    await seed_partition(engine, clock.period_month(TEST_MONTH_START))
    await publish(
        redis, settings.usage_stream_key, burst(customer_id, api_key_id, TEST_MONTH_START, 30)
    )

    await drain.run_once(block_ms=0)
    assert await redis.xlen(settings.usage_stream_key) == 30, "acked, but still in the stream"

    trimmed = await drain.trim_drained()

    assert trimmed >= 29
    assert await redis.xlen(settings.usage_stream_key) <= 1
    assert await drain.outstanding() == 0, "trimming must not confuse the undrained count"


async def test_a_pending_batch_is_never_trimmed_away(settings, engine, redis, drain):
    """The one trim that would turn "redelivered on restart" into "lost": deleting entries
    a crashed worker is still holding. `MINID` is anchored on the oldest pending entry."""
    customer_id, api_key_id = await seed_customer(engine)
    await seed_partition(engine, clock.period_month(TEST_MONTH_START))
    await publish(
        redis, settings.usage_stream_key, burst(customer_id, api_key_id, TEST_MONTH_START, 10)
    )
    await drain.run_once(block_ms=0)  # acked
    later = TEST_MONTH_START + timedelta(hours=1)
    await publish(redis, settings.usage_stream_key, burst(customer_id, api_key_id, later, 10))
    held = await _deliver_without_acking(redis, settings, 10)  # and then the process dies
    assert len(held) == 10

    await drain.trim_drained()

    assert await redis.xlen(settings.usage_stream_key) == 10
    restarted = Drain(settings, engine, redis)
    result = await restarted.run_once(block_ms=0)
    assert result.entries_read == 10
    assert result.rows_inserted == 10


async def test_the_stream_bound_alerts_before_it_is_reached(settings, engine, redis, drain):
    customer_id, api_key_id = await seed_customer(engine)
    await publish(
        redis, settings.usage_stream_key, burst(customer_id, api_key_id, TEST_MONTH_START, 10)
    )

    tight = Settings(
        **{
            **settings.model_dump(),
            "usage_stream_max_entries": 10,
            "usage_stream_alert_ratio": 0.8,
        }
    )
    length, alerting = await Drain(tight, engine, redis).check_stream_bound()

    assert length == 10
    assert alerting, "the alert must fire well before the bound, not at it"
