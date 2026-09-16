"""Reconciliation, rollups, thresholds and the counter rebuild.

Four things are proved here, and each one is a number rather than a state:

1. **Reconciliation states a delta.** Redis counter against Postgres truth, with the part
   explained by the undrained buffer separated from the part that is not. `unexplained`
   is the number ADR-0010 gates the invoice run on.
2. **Rollups are correct at write time** (ADR-0016), at the full grain -- day, segment,
   price list version and API key -- and re-running aggregation does not move them. That
   matters more than it looks: after 90 days there is nothing left to recompute from.
3. **Thresholds come from the domain**, net of prorated fees (ADR-0008 + 0012), and land
   in the Redis key the hot path reads.
4. **Counters rebuild from Postgres, and the rebuild is timed** (ADR-0011). The ADR names
   rebuild duration as its main unmeasured risk, so the test prints the number.

The seeding helpers live in `test_drain.py` -- see the note at the top of that module.
"""

# Fixtures imported from another test module and then named as test parameters look like
# redefinitions to ruff. They are pytest's ordinary cross-module fixture sharing, which
# would normally live in a conftest this agent does not own.
# ruff: noqa: F811

import time
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from meter import billing_calendar as clock
from meter.domain.catalogue import GROWTH_V1, SCALE_V1
from meter.pipeline import aggregate, counters, keys, reconcile, thresholds
from meter.storage.repositories import periods, rollups
from tests.integration.test_drain import (  # noqa: F401  (fixtures are used by name)
    burst,
    drain,
    engine,
    make_record,
    publish,
    redis,
    seed_assignment,
    seed_customer,
    seed_limit,
    seed_partition,
    seed_price_list,
    settings,
)

pytestmark = pytest.mark.integration

# April 2026 in Asia/Karachi: 30 days, and no live traffic anywhere near it.
MONTH = date(2026, 4, 1)
MONTH_START = clock.month_start(MONTH)
MONTH_END = clock.month_end(MONTH)
# 04:00 UTC on the 10th is 09:00 local -- unambiguously the local 10th, not the 9th.
DAY_10 = datetime(2026, 4, 10, 4, 0, tzinfo=UTC)


async def set_counter(redis, customer_id: str, value: int) -> None:
    await redis.set(keys.usage_count(customer_id, MONTH), value)


# ---------------------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------------------


async def test_a_fully_drained_period_reconciles_to_zero(settings, engine, redis, drain):
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    await seed_assignment(engine, customer_id, version_id, MONTH_START)
    await seed_partition(engine, MONTH)

    records = burst(customer_id, api_key_id, DAY_10, 50)
    await publish(redis, settings.usage_stream_key, records)
    await set_counter(redis, customer_id, 50)  # what the hot path INCRed

    await drain.drain_until_empty()
    await aggregate.aggregate_period(engine, customer_id, MONTH)

    report = await reconcile.reconcile(engine, redis, customer_id, MONTH, drainer=drain)

    assert report.redis_counter == 50
    assert report.events_billable == 50
    assert report.rollup_billable == 50
    assert report.stream_outstanding == 0
    assert report.counter_delta == 0
    assert report.rollup_delta == 0
    assert report.unexplained == 0
    assert report.converged
    # The report carries its own evidence -- the number never travels without the query.
    assert "FROM usage_events" in report.explain()
    assert "FROM usage_rollups" in report.explain()


async def test_undrained_usage_is_explained_rather_than_tolerated(
    settings, engine, redis, drain
):
    """The live figure running ahead of the exact one is normal. Reconciliation must
    ACCOUNT for the whole difference, not shrug at it -- so `unexplained` is zero while
    `counter_delta` is not."""
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    await seed_assignment(engine, customer_id, version_id, MONTH_START)
    await seed_partition(engine, MONTH)

    records = burst(customer_id, api_key_id, DAY_10, 30)
    await publish(redis, settings.usage_stream_key, records[:20])
    await drain.drain_until_empty()
    await aggregate.aggregate_period(engine, customer_id, MONTH)
    # Ten more requests captured but not yet drained.
    await publish(redis, settings.usage_stream_key, records[20:])
    await set_counter(redis, customer_id, 30)

    report = await reconcile.reconcile(engine, redis, customer_id, MONTH, drainer=drain)

    assert report.redis_counter == 30
    assert report.events_billable == 20
    assert report.counter_delta == 10
    assert report.stream_outstanding == 10
    assert report.unexplained == 0, "the buffer accounts for every one of the ten"
    assert not report.converged

    await drain.drain_until_empty()
    await aggregate.aggregate_period(engine, customer_id, MONTH)
    assert (
        await reconcile.reconcile(engine, redis, customer_id, MONTH, drainer=drain)
    ).converged


async def test_usage_with_no_plan_assignment_is_reported_not_hidden(
    settings, engine, redis, drain
):
    """A request served to a customer on no plan is real usage that cannot be rated.

    It would otherwise look exactly like aggregation lag in the rollup delta, and lag
    resolves itself in seconds while this never does.
    """
    customer_id, api_key_id = await seed_customer(engine)
    await seed_partition(engine, MONTH)  # note: no plan assignment at all
    records = burst(customer_id, api_key_id, DAY_10, 7)
    await publish(redis, settings.usage_stream_key, records)
    await set_counter(redis, customer_id, 7)

    await drain.drain_until_empty()
    await aggregate.aggregate_period(engine, customer_id, MONTH)
    report = await reconcile.reconcile(engine, redis, customer_id, MONTH, drainer=drain)

    assert report.events_billable == 7
    assert report.rollup_billable == 0
    assert report.unattributed == 7
    assert "UNATTRIBUTED" in report.explain()
    assert not report.converged


# ---------------------------------------------------------------------------------------
# Rollups (ADR-0016)
# ---------------------------------------------------------------------------------------


async def test_the_rollup_grain_separates_days_keys_and_segments(
    settings, engine, redis, drain
):
    """ADR-0016 chose the grain generously so questions asked in April about March are
    still answerable. Prove every axis of it actually separates."""
    customer_id, first_key = await seed_customer(engine)
    async with engine.begin() as conn:
        second_key = str(
            await conn.scalar(
                text(
                    "INSERT INTO api_keys (customer_id, key_hash, prefix) "
                    "VALUES (:customer_id, :key_hash, 'mk_two') RETURNING id"
                ),
                {
                    "customer_id": customer_id,
                    "key_hash": f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
                },
            )
        )

    growth = await seed_price_list(engine, GROWTH_V1)
    scale = await seed_price_list(engine, SCALE_V1)
    change = clock.month_start(MONTH) + timedelta(days=17)  # local 18th belongs to Scale
    await seed_assignment(engine, customer_id, growth, MONTH_START, change)
    await seed_assignment(engine, customer_id, scale, change, MONTH_END)
    await seed_partition(engine, MONTH)

    day_18 = change + timedelta(hours=5)
    records = (
        burst(customer_id, first_key, DAY_10, 5)
        + burst(customer_id, second_key, DAY_10, 3)
        + burst(customer_id, first_key, DAY_10 + timedelta(days=1), 2)
        + burst(customer_id, first_key, day_18, 4)
    )
    await publish(redis, settings.usage_stream_key, records)
    await drain.drain_until_empty()
    await aggregate.aggregate_period(engine, customer_id, MONTH)

    async with engine.connect() as conn:
        period = await periods.get_period(conn, customer_id, MONTH)
        rows = (
            await conn.execute(
                text(
                    "SELECT usage_date, api_key_id, plan_assignment_id, "
                    "       price_list_version_id, billable_requests "
                    "  FROM usage_rollups WHERE billing_period_id = :period_id "
                    " ORDER BY usage_date, billable_requests DESC"
                ),
                {"period_id": period.id},
            )
        ).all()

    # 10 Apr / key one, 10 Apr / key two, 11 Apr / key one, 18 Apr / key one (new segment)
    assert len(rows) == 4
    assert [int(row.billable_requests) for row in rows] == [5, 3, 2, 4]
    assert {str(row.usage_date) for row in rows} == {"2026-04-10", "2026-04-11", "2026-04-18"}
    assert len({str(row.api_key_id) for row in rows}) == 2
    assert len({str(row.plan_assignment_id) for row in rows}) == 2
    assert len({str(row.price_list_version_id) for row in rows}) == 2

    async with engine.connect() as conn:
        assert await rollups.rollups_billable(conn, customer_id, period.id) == 14


async def test_re_aggregating_is_a_no_op(settings, engine, redis, drain):
    """The upsert ASSIGNS rather than adds, which is what makes the whole pipeline
    replayable. If it added, a redelivered batch or a re-run pass would inflate the bill."""
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    await seed_assignment(engine, customer_id, version_id, MONTH_START)
    await seed_partition(engine, MONTH)
    records = burst(customer_id, api_key_id, DAY_10, 11)
    await publish(redis, settings.usage_stream_key, records)
    await drain.drain_until_empty()

    for _ in range(4):
        await aggregate.aggregate_period(engine, customer_id, MONTH)

    async with engine.connect() as conn:
        period = await periods.get_period(conn, customer_id, MONTH)
        assert await rollups.rollups_billable(conn, customer_id, period.id) == 11


async def test_the_incremental_pass_aggregates_what_the_drain_touched(
    settings, engine, redis, drain
):
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    await seed_assignment(engine, customer_id, version_id, MONTH_START)
    await seed_partition(engine, MONTH)
    records = burst(customer_id, api_key_id, DAY_10, 9)
    await publish(redis, settings.usage_stream_key, records)
    await drain.drain_until_empty()

    # The live worker shares the dirty set and may have aggregated these already; either
    # way the result is the same, which is the point of an idempotent pass.
    await aggregate.aggregate_dirty(engine, redis)

    async with engine.connect() as conn:
        period = await periods.get_period(conn, customer_id, MONTH)
        assert await rollups.rollups_billable(conn, customer_id, period.id) == 9


# ---------------------------------------------------------------------------------------
# Thresholds (ADR-0008 + ADR-0012)
# ---------------------------------------------------------------------------------------


async def test_the_threshold_is_the_domain_inversion_of_the_whole_bill(
    settings, engine, redis, drain
):
    """ADR-0012's own worked example: Growth, Rs. 15,000 fee, a Rs. 50,000 limit.

    Rs. 35,000 of headroom at Rs. 0.50 is 70,000 chargeable requests, on top of 500,000
    included -- so 570,000. The fee counts toward the limit first.
    """
    customer_id, _ = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    await seed_assignment(engine, customer_id, version_id, MONTH_START)
    async with engine.begin() as conn:
        period = await periods.ensure_period(conn, customer_id, MONTH)
    await seed_limit(engine, customer_id, period.id, 5_000_000)  # Rs. 50,000

    threshold = await thresholds.recompute_for(engine, redis, customer_id, MONTH)

    assert threshold is not None
    assert threshold.threshold_requests == 570_000
    assert threshold.fee_component_paisa == 1_500_000
    # Written where the hot path reads it, as a plain integer.
    assert int(await redis.get(keys.limit_threshold(customer_id, MONTH))) == 570_000


async def test_a_partial_period_prorates_the_threshold(settings, engine, redis, drain):
    """ADR-0017: a mid-month signup prorates exactly like a plan change -- fee, allowance
    and band widths. The threshold must be computed against the prorated ladder, or a new
    customer gets a full month's headroom for a fortnight's fee."""
    customer_id, _ = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    signup = MONTH_START + timedelta(days=15)  # 15 of 30 days
    await seed_assignment(engine, customer_id, version_id, signup)
    async with engine.begin() as conn:
        period = await periods.ensure_period(conn, customer_id, MONTH)
    await seed_limit(engine, customer_id, period.id, 5_000_000)

    threshold = await thresholds.recompute_for(engine, redis, customer_id, MONTH)

    # Half the fee (Rs. 7,500) leaves Rs. 42,500 of headroom at Rs. 0.50 = 85,000
    # chargeable, on top of a prorated allowance of 250,000.
    assert threshold.fee_component_paisa == 750_000
    assert threshold.threshold_requests == 250_000 + 85_000


async def test_an_unsatisfiable_limit_yields_a_threshold_of_zero(
    settings, engine, redis, drain
):
    """ADR-0012 rejects a limit below the monthly fee when it is SET. If one reaches the
    pipeline anyway, the honest threshold is zero -- refused from the first request -- and
    never a number that quietly lets the fee be exceeded."""
    customer_id, _ = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    await seed_assignment(engine, customer_id, version_id, MONTH_START)
    async with engine.begin() as conn:
        period = await periods.ensure_period(conn, customer_id, MONTH)
    await seed_limit(engine, customer_id, period.id, 1_000_000)  # Rs. 10,000 < Rs. 15,000

    threshold = await thresholds.recompute_for(engine, redis, customer_id, MONTH)

    assert threshold.threshold_requests == 0


async def test_the_sweep_recomputes_only_what_has_moved(settings, engine, redis, drain):
    """A missed recomputation is a silent enforcement failure (ADR-0008), so the sweep must
    catch a changed input -- and must not thrash on its own writes."""
    customer_id, _ = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    await seed_assignment(engine, customer_id, version_id, MONTH_START)
    async with engine.begin() as conn:
        period = await periods.ensure_period(conn, customer_id, MONTH)
    await seed_limit(engine, customer_id, period.id, 5_000_000)

    first = await thresholds.sweep(engine, redis, max_age_seconds=3_600)
    assert first.recomputed >= 1

    # Nothing has changed: this customer must not be recomputed again.
    async with engine.connect() as conn:
        before = (
            await conn.execute(
                text(
                    "SELECT threshold_computed_at FROM spending_limits "
                    " WHERE customer_id = :customer_id"
                ),
                {"customer_id": customer_id},
            )
        ).scalar_one()
    await thresholds.sweep(engine, redis, max_age_seconds=3_600)
    async with engine.connect() as conn:
        after = (
            await conn.execute(
                text(
                    "SELECT threshold_computed_at FROM spending_limits "
                    " WHERE customer_id = :customer_id"
                ),
                {"customer_id": customer_id},
            )
        ).scalar_one()
    assert before == after, "the sweep must not recompute a threshold nothing has moved"

    # Now change the limit. The sweep must notice.
    await seed_limit(engine, customer_id, period.id, 2_500_000)  # Rs. 25,000
    await thresholds.sweep(engine, redis, max_age_seconds=3_600)
    assert int(await redis.get(keys.limit_threshold(customer_id, MONTH))) == 500_000 + 20_000


async def test_a_plan_change_moves_the_threshold(settings, engine, redis, drain):
    """ADR-0012's sharpest edge: an upgrade's larger prorated fee eats the same cap, so
    remaining headroom can SHRINK as a direct result of asking for more capacity."""
    customer_id, _ = await seed_customer(engine)
    growth = await seed_price_list(engine, GROWTH_V1)
    scale = await seed_price_list(engine, SCALE_V1)
    await seed_assignment(engine, customer_id, growth, MONTH_START)
    async with engine.begin() as conn:
        period = await periods.ensure_period(conn, customer_id, MONTH)
    await seed_limit(engine, customer_id, period.id, 10_000_000)  # Rs. 100,000

    before = await thresholds.recompute_for(engine, redis, customer_id, MONTH)

    change = MONTH_START + timedelta(days=15)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE plan_assignments SET effective = tstzrange(lower(effective), "
                ":change, '[)') WHERE customer_id = :customer_id"
            ),
            {"change": change, "customer_id": customer_id},
        )
    await seed_assignment(engine, customer_id, scale, change, MONTH_END)

    after = await thresholds.recompute_for(engine, redis, customer_id, MONTH)

    assert after.fee_component_paisa > before.fee_component_paisa, (
        "half a Growth fee plus half a Scale fee exceeds a whole Growth fee"
    )
    assert after.threshold_requests != before.threshold_requests
    assert int(await redis.get(keys.limit_threshold(customer_id, MONTH))) == (
        after.threshold_requests
    )


# ---------------------------------------------------------------------------------------
# Counter rebuild (ADR-0011) -- and the number the ADR says is missing
# ---------------------------------------------------------------------------------------


async def test_counters_rebuild_from_postgres_after_an_empty_redis(
    settings, engine, redis, drain
):
    """ADR-0011's scenario: Redis restarts on the 19th with an empty keyspace.

    The counters must come back from the system of record, the thresholds with them, and
    the authoritative marker LAST -- because the hot path serves on that marker, and
    serving with a counter but no threshold enforces nothing.
    """
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    await seed_assignment(engine, customer_id, version_id, MONTH_START)
    await seed_partition(engine, MONTH)

    records = burst(customer_id, api_key_id, DAY_10, 37)
    await publish(redis, settings.usage_stream_key, records)
    await drain.drain_until_empty()
    await aggregate.aggregate_period(engine, customer_id, MONTH)
    async with engine.connect() as conn:
        period = await periods.get_period(conn, customer_id, MONTH)
    await seed_limit(engine, customer_id, period.id, 5_000_000)

    # Redis restarts empty: the counter, the threshold and the marker are all gone.
    await redis.delete(
        keys.usage_count(customer_id, MONTH),
        keys.limit_threshold(customer_id, MONTH),
    )
    assert await redis.get(keys.usage_count(customer_id, MONTH)) is None

    result = await counters.rebuild(engine, redis, MONTH)

    assert int(await redis.get(keys.usage_count(customer_id, MONTH))) == 37
    assert int(await redis.get(keys.limit_threshold(customer_id, MONTH))) == 570_000
    assert await counters.is_authoritative(redis)
    assert result.seconds > 0
    # Published to Redis at microsecond precision, so operations can see the number
    # without reading a log line.
    assert float(await redis.get(keys.COUNTERS_REBUILD_SECONDS)) == pytest.approx(
        result.seconds, abs=1e-5
    )
    # And it reconciles to zero immediately afterwards, which is the actual claim.
    report = await reconcile.reconcile(engine, redis, customer_id, MONTH, drainer=drain)
    assert report.counter_delta == 0


async def test_rebuilding_twice_produces_the_same_keyspace(settings, engine, redis, drain):
    """A rebuild SETs absolute values from the system of record rather than incrementing,
    so running it twice is not a way to double a customer's counter."""
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    await seed_assignment(engine, customer_id, version_id, MONTH_START)
    await seed_partition(engine, MONTH)
    records = burst(customer_id, api_key_id, DAY_10, 13)
    await publish(redis, settings.usage_stream_key, records)
    await drain.drain_until_empty()

    await counters.rebuild(engine, redis, MONTH)
    await counters.rebuild(engine, redis, MONTH)

    assert int(await redis.get(keys.usage_count(customer_id, MONTH))) == 13


@pytest.mark.parametrize("customer_count,requests_each", [(50, 200)])
async def test_measure_the_rebuild_time(
    settings, engine, redis, drain, customer_count, requests_each, capsys
):
    """ADR-0011: "rebuild time from Postgres is the number that matters and is currently
    unknown". This measures it, on a month whose usage this test wrote itself.

    The shape matters more than the absolute figure on a laptop: the rebuild is one grouped
    scan of one partition plus one pipelined MSET, so it is linear in CUSTOMERS and only
    weakly sensitive to request volume. That is what the printed number should be read as.
    """
    # A month of its own, so nothing else in the database is in the measurement.
    month = date(2026, 5, 1)
    month_start = clock.month_start(month)
    await seed_partition(engine, month)
    version_id = await seed_price_list(engine, GROWTH_V1)
    occurred = month_start + timedelta(days=9, hours=9)

    for index in range(customer_count):
        customer_id, api_key_id = await seed_customer(engine, name=f"Bench {index}")
        await seed_assignment(engine, customer_id, version_id, month_start)
        rows = [
            make_record(customer_id, api_key_id, occurred + timedelta(seconds=n)).as_row()
            for n in range(requests_each)
        ]
        async with engine.begin() as conn:
            await rollups.insert_usage_events(conn, rows)

    started = time.perf_counter()
    result = await counters.rebuild(engine, redis, month)
    measured = time.perf_counter() - started

    assert result.customers >= customer_count
    assert result.requests >= customer_count * requests_each
    with capsys.disabled():
        print(f"\n  ADR-0011 rebuild measurement: {result.explain()}")
        print(f"  wall clock including the call: {measured * 1000:.0f}ms")
    # Not a performance assertion -- a regression tripwire. A rebuild that takes minutes
    # means a Redis restart is minutes of full outage, and ADR-0011 says to revisit the
    # decision rather than defend it.
    assert result.seconds < 30
