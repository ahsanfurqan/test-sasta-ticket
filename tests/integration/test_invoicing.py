"""Month close and invoice generation. ADR-0010, ADR-0013, and the brief's worked example.

What is proved here:

* the published worked example comes out **exact to the rupee** through the real path --
  rollups in, `meter.domain` rating, invoice lines out;
* **an issued invoice is immutable**, and the database is what enforces it -- regeneration
  either reproduces the number or raises, and never updates;
* **late usage rolls forward**, labelled, priced at the price list version in effect when
  it was incurred, onto the NEXT invoice;
* **a mid-month plan change** produces one set of lines per segment, each against its own
  prorated allowance and ladder;
* **the grace window fallback** issues anyway and records the shortfall loudly, rather
  than issuing a quietly short invoice.

Seeding helpers come from `test_drain.py` -- see the note at the top of that module.
"""

# Fixtures imported from another test module and then named as test parameters look like
# redefinitions to ruff. This is pytest's ordinary cross-module fixture sharing, which
# would normally live in a conftest this agent does not own.
# ruff: noqa: F811

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from meter.config import get_settings
from meter.domain.catalogue import GROWTH_V1, SCALE_V1, STARTER_V1
from meter.pipeline import clock, close, invoicing, keys
from meter.storage.repositories import invoices, periods
from tests.integration.test_drain import (  # noqa: F401  (fixtures are used by name)
    burst,
    drain,
    engine,
    publish,
    redis,
    seed_assignment,
    seed_customer,
    seed_partition,
    seed_price_list,
    settings,
)

pytestmark = pytest.mark.integration


JANUARY = date(2026, 1, 1)
FEBRUARY = date(2026, 2, 1)
JUNE = date(2026, 6, 1)
JULY = date(2026, 7, 1)


async def seed_rollup(
    engine,
    *,
    customer_id: str,
    period_id: str,
    usage_date: date,
    version_id: str,
    assignment_id: str,
    api_key_id: str,
    billable: int,
) -> None:
    """Write a rollup directly.

    Aggregation from per-request rows is proved in `test_reconciliation.py`; these tests
    are about what happens to a rollup once it exists, and seeding 1.2 million usage rows
    to re-prove the aggregation would be measuring the wrong thing.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO usage_rollups (customer_id, billing_period_id, usage_date, "
                "  price_list_version_id, plan_assignment_id, api_key_id, billable_requests) "
                "VALUES (:customer_id, :period_id, :usage_date, :version_id, "
                "        :assignment_id, :api_key_id, :billable) "
                "ON CONFLICT (customer_id, usage_date, plan_assignment_id, "
                "             price_list_version_id, api_key_id) "
                "DO UPDATE SET billable_requests = EXCLUDED.billable_requests"
            ),
            {
                "customer_id": customer_id,
                "period_id": period_id,
                "usage_date": usage_date,
                "version_id": version_id,
                "assignment_id": assignment_id,
                "api_key_id": api_key_id,
                "billable": billable,
            },
        )


async def open_period(engine, customer_id: str, month: date):
    async with engine.begin() as conn:
        return await periods.ensure_period(conn, customer_id, month)


# ---------------------------------------------------------------------------------------
# The worked example
# ---------------------------------------------------------------------------------------


async def test_the_published_worked_example_is_exact_to_the_rupee(engine):
    """CLAUDE.md: Growth, 1,200,000 requests ->
        15,000 fee + 0 (first 500k included) + 500,000 x 0.50 + 200,000 x 0.35
      = Rs. 335,000.00
    """
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    assignment_id = await seed_assignment(
        engine, customer_id, version_id, clock.month_start(JUNE), clock.month_end(JUNE)
    )
    period = await open_period(engine, customer_id, JUNE)
    await seed_rollup(
        engine,
        customer_id=customer_id,
        period_id=period.id,
        usage_date=date(2026, 6, 15),
        version_id=version_id,
        assignment_id=assignment_id,
        api_key_id=api_key_id,
        billable=1_200_000,
    )

    result = await invoicing.generate(engine, customer_id, JUNE)

    assert result.total_paisa == 33_500_000  # Rs. 335,000.00
    assert "Rs. 335,000.00" in result.explain()

    # Every line is a re-derivable multiplication, and they sum to the total.
    assert sum(line.amount_paisa for line in result.lines) == result.total_paisa
    for line in result.lines:
        assert line.amount_paisa == line.quantity * line.unit_price_paisa
    amounts = {line.kind: line.amount_paisa for line in result.lines if line.kind == "monthly_fee"}
    assert amounts["monthly_fee"] == 1_500_000
    usage_lines = [line for line in result.lines if line.kind == "usage"]
    assert [(line.quantity, line.unit_price_paisa) for line in usage_lines] == [
        (500_000, 0),  # the included allowance, priced at zero -- band zero
        (500_000, 50),
        (200_000, 35),
    ]


async def test_the_invoice_is_issued_and_the_period_is_marked(engine):
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, STARTER_V1)
    assignment_id = await seed_assignment(
        engine, customer_id, version_id, clock.month_start(JUNE), clock.month_end(JUNE)
    )
    period = await open_period(engine, customer_id, JUNE)
    await seed_rollup(
        engine,
        customer_id=customer_id,
        period_id=period.id,
        usage_date=date(2026, 6, 3),
        version_id=version_id,
        assignment_id=assignment_id,
        api_key_id=api_key_id,
        billable=10_050,
    )

    result = await invoicing.generate(engine, customer_id, JUNE)

    assert result.total_paisa == 50 * 80  # Rs. 40.00, Starter's fee is zero
    async with engine.connect() as conn:
        stored = await invoices.get_for_period(conn, customer_id, period.id)
        refreshed = await periods.get_period(conn, customer_id, JUNE)
        rows = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM usage_rollups "
                    " WHERE billing_period_id = :id AND invoice_id IS NOT NULL"
                ),
                {"id": period.id},
            )
        ).scalar_one()
    assert stored.status == "issued"
    assert stored.issued_at is not None
    assert stored.total_paisa == result.total_paisa
    assert refreshed.status == "invoiced"
    assert rows == 1, "the rollups that were billed carry the invoice that billed them"


# ---------------------------------------------------------------------------------------
# Immutability (ADR-0013)
# ---------------------------------------------------------------------------------------


async def test_regenerating_an_unchanged_invoice_produces_the_same_number(engine):
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, GROWTH_V1)
    assignment_id = await seed_assignment(
        engine, customer_id, version_id, clock.month_start(JUNE), clock.month_end(JUNE)
    )
    period = await open_period(engine, customer_id, JUNE)
    await seed_rollup(
        engine,
        customer_id=customer_id,
        period_id=period.id,
        usage_date=date(2026, 6, 9),
        version_id=version_id,
        assignment_id=assignment_id,
        api_key_id=api_key_id,
        billable=700_000,
    )

    first = await invoicing.generate(engine, customer_id, JUNE)
    second = await invoicing.generate(engine, customer_id, JUNE)

    assert second.already_existed
    assert second.invoice_id == first.invoice_id
    assert second.invoice_number == first.invoice_number
    assert second.total_paisa == first.total_paisa


async def test_regenerating_after_late_usage_fails_loudly_and_writes_nothing(engine):
    """The dangerous case. Late usage makes a recomputation disagree with what Finance
    already sent, and the only correct response is to refuse -- the difference belongs on
    the NEXT invoice, which the roll-forward test below proves it reaches."""
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, STARTER_V1)
    assignment_id = await seed_assignment(
        engine, customer_id, version_id, clock.month_start(JUNE), clock.month_end(JUNE)
    )
    period = await open_period(engine, customer_id, JUNE)
    seed = dict(
        customer_id=customer_id,
        period_id=period.id,
        usage_date=date(2026, 6, 9),
        version_id=version_id,
        assignment_id=assignment_id,
        api_key_id=api_key_id,
    )
    await seed_rollup(engine, **seed, billable=10_100)
    issued = await invoicing.generate(engine, customer_id, JUNE)

    await seed_rollup(engine, **seed, billable=10_150)  # 50 late requests land

    with pytest.raises(invoices.InvoiceImmutable) as caught:
        await invoicing.generate(engine, customer_id, JUNE)
    assert "immutable" in str(caught.value)

    async with engine.connect() as conn:
        unchanged = await invoices.get_for_period(conn, customer_id, period.id)
    assert unchanged.total_paisa == issued.total_paisa, "not one paisa moved"


async def test_the_database_refuses_to_move_an_issued_total(engine):
    """Application code is not the last line of defence (ADR-0013). Prove the trigger."""
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, STARTER_V1)
    assignment_id = await seed_assignment(
        engine, customer_id, version_id, clock.month_start(JUNE), clock.month_end(JUNE)
    )
    period = await open_period(engine, customer_id, JUNE)
    await seed_rollup(
        engine,
        customer_id=customer_id,
        period_id=period.id,
        usage_date=date(2026, 6, 11),
        version_id=version_id,
        assignment_id=assignment_id,
        api_key_id=api_key_id,
        billable=10_010,
    )
    result = await invoicing.generate(engine, customer_id, JUNE)

    with pytest.raises(DBAPIError) as caught:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE invoices SET total_paisa = 1 WHERE id = :id"),
                {"id": result.invoice_id},
            )
    assert "immutable" in str(caught.value)


# ---------------------------------------------------------------------------------------
# Roll-forward (ADR-0010)
# ---------------------------------------------------------------------------------------


async def test_late_usage_rolls_forward_onto_the_next_invoice(engine):
    """ADR-0010's own sentence, as a test: "12,400 requests from November, received after
    that invoice was issued" -- priced at the version in effect when it was incurred."""
    customer_id, api_key_id = await seed_customer(engine)
    old_version = await seed_price_list(engine, STARTER_V1)
    january_assignment = await seed_assignment(
        engine,
        customer_id,
        old_version,
        clock.month_start(JANUARY),
        clock.month_start(FEBRUARY),
    )
    january = await open_period(engine, customer_id, JANUARY)
    january_seed = dict(
        customer_id=customer_id,
        period_id=january.id,
        usage_date=date(2026, 1, 20),
        version_id=old_version,
        assignment_id=january_assignment,
        api_key_id=api_key_id,
    )
    await seed_rollup(engine, **january_seed, billable=10_100)
    january_invoice = await invoicing.generate(engine, customer_id, JANUARY)
    assert january_invoice.total_paisa == 100 * 80

    # A NEW price list version for February -- the roll-forward must NOT use it.
    new_version = await seed_price_list(engine, GROWTH_V1)
    february_assignment = await seed_assignment(
        engine, customer_id, new_version, clock.month_start(FEBRUARY), clock.month_end(FEBRUARY)
    )
    february = await open_period(engine, customer_id, FEBRUARY)
    await seed_rollup(
        engine,
        customer_id=customer_id,
        period_id=february.id,
        usage_date=date(2026, 2, 14),
        version_id=new_version,
        assignment_id=february_assignment,
        api_key_id=api_key_id,
        billable=400,
    )
    # ... and 40 January requests arrive after January's invoice was issued.
    await seed_rollup(engine, **january_seed, billable=10_140)

    february_invoice = await invoicing.generate(engine, customer_id, FEBRUARY)

    prior = [line for line in february_invoice.lines if line.kind == "prior_period_usage"]
    assert len(prior) == 1
    assert prior[0].quantity == 40
    assert prior[0].unit_price_paisa == 80, "January's price, not February's"
    assert prior[0].amount_paisa == 40 * 80
    assert prior[0].usage_period_id == january.id
    assert prior[0].price_list_version_id == old_version
    assert "January 2026" in prior[0].description
    assert "received after that invoice was issued" in prior[0].description

    assert february_invoice.prior_period_requests == 40
    assert february_invoice.total_paisa == 1_500_000 + 40 * 80  # Growth fee + roll-forward

    # And January's invoice never moved.
    async with engine.connect() as conn:
        untouched = await invoices.get_for_period(conn, customer_id, january.id)
    assert untouched.total_paisa == january_invoice.total_paisa


async def test_a_rolled_forward_charge_is_not_billed_twice(engine):
    """The roll-forward is a difference, so once it has been billed it stops recurring.

    Getting this wrong bills the same late requests every month forever, which is a worse
    failure than losing them.
    """
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, STARTER_V1)
    january_assignment = await seed_assignment(
        engine, customer_id, version_id, clock.month_start(JANUARY), clock.month_start(FEBRUARY)
    )
    january = await open_period(engine, customer_id, JANUARY)
    january_seed = dict(
        customer_id=customer_id,
        period_id=january.id,
        usage_date=date(2026, 1, 20),
        version_id=version_id,
        assignment_id=january_assignment,
        api_key_id=api_key_id,
    )
    await seed_rollup(engine, **january_seed, billable=10_100)
    await invoicing.generate(engine, customer_id, JANUARY)
    await seed_rollup(engine, **january_seed, billable=10_130)  # 30 late

    february_assignment = await seed_assignment(
        engine, customer_id, version_id, clock.month_start(FEBRUARY), clock.month_end(FEBRUARY)
    )
    february = await open_period(engine, customer_id, FEBRUARY)
    await seed_rollup(
        engine,
        customer_id=customer_id,
        period_id=february.id,
        usage_date=date(2026, 2, 14),
        version_id=version_id,
        assignment_id=february_assignment,
        api_key_id=api_key_id,
        billable=5,
    )
    february_invoice = await invoicing.generate(engine, customer_id, FEBRUARY)
    assert february_invoice.prior_period_requests == 30

    march = await open_period(engine, customer_id, date(2026, 3, 1))
    march_assignment = await seed_assignment(
        engine,
        customer_id,
        version_id,
        clock.month_start(date(2026, 3, 1)),
        clock.month_end(date(2026, 3, 1)),
    )
    await seed_rollup(
        engine,
        customer_id=customer_id,
        period_id=march.id,
        usage_date=date(2026, 3, 2),
        version_id=version_id,
        assignment_id=march_assignment,
        api_key_id=api_key_id,
        billable=3,
    )
    march_invoice = await invoicing.generate(engine, customer_id, date(2026, 3, 1))

    assert march_invoice.prior_period_requests == 0, "January's 30 were settled in February"
    assert not [line for line in march_invoice.lines if line.kind == "prior_period_usage"]


# ---------------------------------------------------------------------------------------
# Plan changes (ADR-0006 / ADR-0017)
# ---------------------------------------------------------------------------------------


async def test_a_mid_month_plan_change_bills_two_segments(engine):
    """Each segment is rated against its OWN prorated fee, allowance and band ladder.

    The invoice must carry both, each citing the version and the assignment it came from,
    because that is what makes the charge re-derivable rather than archaeological.
    """
    customer_id, api_key_id = await seed_customer(engine)
    growth = await seed_price_list(engine, GROWTH_V1)
    scale = await seed_price_list(engine, SCALE_V1)
    start = clock.month_start(JULY)
    change = start + timedelta(days=17)  # the local 18th belongs to Scale
    growth_assignment = await seed_assignment(engine, customer_id, growth, start, change)
    scale_assignment = await seed_assignment(
        engine, customer_id, scale, change, clock.month_end(JULY)
    )
    period = await open_period(engine, customer_id, JULY)
    for assignment, version, day, quantity in (
        (growth_assignment, growth, date(2026, 7, 5), 400_000),
        (scale_assignment, scale, date(2026, 7, 20), 100_000),
    ):
        await seed_rollup(
            engine,
            customer_id=customer_id,
            period_id=period.id,
            usage_date=day,
            version_id=version,
            assignment_id=assignment,
            api_key_id=api_key_id,
            billable=quantity,
        )

    result = await invoicing.generate(engine, customer_id, JULY)

    fees = [line for line in result.lines if line.kind == "monthly_fee"]
    assert len(fees) == 2, "one prorated fee per segment"
    # 17/31 of Rs. 15,000 and 14/31 of Rs. 90,000, both rounded DOWN (the customer wins).
    assert fees[0].amount_paisa == 1_500_000 * 17 // 31
    assert fees[1].amount_paisa == 9_000_000 * 14 // 31
    assert {line.plan_assignment_id for line in result.lines} == {
        growth_assignment,
        scale_assignment,
    }
    assert {line.price_list_version_id for line in result.lines} == {growth, scale}
    assert "for 17 of 31 days" in result.explain()
    assert sum(line.amount_paisa for line in result.lines) == result.total_paisa


# ---------------------------------------------------------------------------------------
# Month close: reconcile, then issue (ADR-0010)
# ---------------------------------------------------------------------------------------


async def test_close_drains_aggregates_reconciles_then_issues(settings, engine, redis, drain):
    """The whole path, end to end, from stream entries to an issued invoice."""
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, STARTER_V1)
    month = date(2026, 2, 1)
    await seed_partition(engine, month)
    await seed_assignment(
        engine, customer_id, version_id, clock.month_start(month), clock.month_end(month)
    )
    occurred = clock.month_start(month) + timedelta(days=6, hours=9)
    records = burst(customer_id, api_key_id, occurred, 40)
    await publish(redis, settings.usage_stream_key, records)
    await redis.set(keys.usage_count(customer_id, month), 40)

    result = await close.close_customer(
        settings, engine, redis, drain, customer_id, month, grace_seconds=5
    )

    assert result.converged, result.explain()
    assert result.reconciliation.unexplained == 0
    assert result.reconciliation.events_billable == 40
    assert result.reconciliation.rollup_billable == 40
    assert result.discrepancy_requests == 0
    assert result.invoice is not None
    assert result.invoice.total_paisa == 0  # 40 requests, all inside Starter's 10,000

    async with engine.connect() as conn:
        period = await periods.get_period(conn, customer_id, month)
    assert period.status == "invoiced"
    assert period.reconciled_at is not None or period.closed_at is not None


async def test_close_is_safe_to_run_twice(settings, engine, redis, drain):
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, STARTER_V1)
    month = date(2026, 2, 1)
    await seed_partition(engine, month)
    await seed_assignment(
        engine, customer_id, version_id, clock.month_start(month), clock.month_end(month)
    )
    occurred = clock.month_start(month) + timedelta(days=6, hours=9)
    await publish(redis, settings.usage_stream_key, burst(customer_id, api_key_id, occurred, 12))
    await redis.set(keys.usage_count(customer_id, month), 12)

    first = await close.close_customer(
        settings, engine, redis, drain, customer_id, month, grace_seconds=5
    )
    second = await close.close_customer(
        settings, engine, redis, drain, customer_id, month, grace_seconds=5
    )

    assert second.invoice.invoice_id == first.invoice.invoice_id
    assert second.invoice.total_paisa == first.invoice.total_paisa
    assert second.invoice.already_existed

    async with engine.connect() as conn:
        period = await periods.get_period(conn, customer_id, month)
    assert period.status == "invoiced", "a re-close must not leave the period looking open"


async def test_the_grace_window_fallback_issues_anyway_and_records_the_shortfall(
    settings, engine, redis, drain
):
    """ADR-0010's fallback. A counter ahead of Postgres with nothing left in the buffer is
    exactly ADR-0018's residual AOF loss: real, unrecoverable, and the one thing that must
    never be issued silently."""
    customer_id, api_key_id = await seed_customer(engine)
    version_id = await seed_price_list(engine, STARTER_V1)
    month = date(2026, 2, 1)
    await seed_partition(engine, month)
    await seed_assignment(
        engine, customer_id, version_id, clock.month_start(month), clock.month_end(month)
    )
    occurred = clock.month_start(month) + timedelta(days=6, hours=9)
    await publish(redis, settings.usage_stream_key, burst(customer_id, api_key_id, occurred, 20))
    # The hot path counted 25; five of them never reached the stream.
    await redis.set(keys.usage_count(customer_id, month), 25)

    result = await close.close_customer(
        settings, engine, redis, drain, customer_id, month, grace_seconds=0
    )

    assert not result.converged
    assert result.reconciliation.counter_delta == 5
    assert result.reconciliation.stream_outstanding == 0
    assert result.reconciliation.unexplained == 5
    assert result.discrepancy_requests == 5
    assert result.invoice is not None, "Finance still gets an invoice"

    async with engine.connect() as conn:
        period = await periods.get_period(conn, customer_id, month)
        note = (
            await conn.execute(
                text("SELECT discrepancy_requests, discrepancy_note FROM billing_periods "
                     " WHERE id = :id"),
                {"id": period.id},
            )
        ).one()
    assert note.discrepancy_requests == 5
    assert "grace window" in note.discrepancy_note


async def test_a_customer_on_no_plan_cannot_be_invoiced(settings, engine, redis, drain):
    """Refusing is the right answer: there is no ladder, so any number would be invented."""
    customer_id, api_key_id = await seed_customer(engine)
    month = date(2026, 2, 1)
    await seed_partition(engine, month)
    occurred = clock.month_start(month) + timedelta(days=6, hours=9)
    await publish(redis, settings.usage_stream_key, burst(customer_id, api_key_id, occurred, 3))

    result = await close.close_customer(
        settings, engine, redis, drain, customer_id, month, grace_seconds=0
    )

    assert result.invoice is None
    assert "no plan assignment" in result.error


async def test_the_invoice_number_is_deterministic(engine):
    customer_id = str(uuid.uuid4())
    assert invoices.invoice_number(customer_id, JUNE) == invoices.invoice_number(
        customer_id, JUNE
    )
    assert invoices.invoice_number(customer_id, JUNE) != invoices.invoice_number(
        customer_id, JULY
    )
    assert invoices.invoice_number(customer_id, JUNE).startswith("INV-202606-")


async def test_settings_expose_the_stream_contract():
    """The one name both sides must spell identically (ADR-0018)."""
    settings = get_settings()
    assert settings.usage_stream_key == "usage:events"
    assert keys.usage_count("c", JUNE) == "usage:count:c:2026-06"
    assert keys.limit_threshold("c", JUNE) == "limit:threshold:c:2026-06"
