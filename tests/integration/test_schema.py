"""Schema invariants, proved against a real Postgres. Owned by data-model.

These are not ORM tests. Every assertion here is about something the DATABASE refuses,
because application code is not the last line of defence when a bug tries to move a number
Finance already sent (ADR-0013) or to "just fix a typo" in a price a past invoice was
computed from (ADR-0005).

Everything runs inside one transaction that is rolled back, with a SAVEPOINT around each
expected failure -- which is also the only way to test an issued invoice at all, since by
construction it can never be deleted afterwards.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from meter.config import get_settings
from meter.storage.db import create_engine
from meter.storage.models import Base

pytestmark = pytest.mark.integration

SHA256_OF_NOTHING = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.fixture
async def conn() -> AsyncIterator[AsyncConnection]:
    """A connection in an open transaction, always rolled back.

    An issued invoice cannot be deleted -- that is the point of this module -- so a test
    that issues one has no way to clean up except by never committing.
    """
    engine = create_engine(get_settings())
    async with engine.connect() as connection:
        await connection.begin()
        try:
            yield connection
        finally:
            await connection.rollback()
    await engine.dispose()


async def refuses(connection: AsyncConnection, sql: str, /, **params: object) -> str:
    """Assert the database rejects this statement, and return the message it rejected with.

    Wrapped in a SAVEPOINT: the failure aborts its subtransaction, not the whole test.
    """
    savepoint = await connection.begin_nested()
    with pytest.raises(DBAPIError) as caught:
        await connection.execute(text(sql), params)
    await savepoint.rollback()
    return str(caught.value)


# ---------------------------------------------------------------------------------------
# Fixtures that build one of everything
# ---------------------------------------------------------------------------------------


async def make_customer(connection: AsyncConnection, name: str = "Acme") -> str:
    return await connection.scalar(
        text("INSERT INTO customers (name) VALUES (:name) RETURNING id"), {"name": name}
    )


async def make_api_key(connection: AsyncConnection, customer_id: str) -> str:
    return await connection.scalar(
        text(
            "INSERT INTO api_keys (customer_id, key_hash, prefix) "
            "VALUES (:customer_id, :key_hash, 'mk_test') RETURNING id"
        ),
        {"customer_id": customer_id, "key_hash": f"{uuid.uuid4().hex}{uuid.uuid4().hex}"},
    )


async def make_price_version(
    connection: AsyncConnection,
    *,
    name: str = "Growth",
    publish: bool = True,
    bands: tuple[tuple[int | None, int], ...] = ((500_000, 50), (None, 35)),
) -> str:
    """A price list with one version. Growth v1 by default: Rs. 15,000, 500k included."""
    list_id = await connection.scalar(
        text("INSERT INTO price_lists (name) VALUES (:name) RETURNING id"),
        {"name": f"{name}-{uuid.uuid4().hex[:8]}"},
    )
    version_id = await connection.scalar(
        text(
            "INSERT INTO price_list_versions "
            "  (price_list_id, version, name, monthly_fee_paisa, included_quantity) "
            "VALUES (:list_id, 1, :name, 1500000, 500000) RETURNING id"
        ),
        {"list_id": list_id, "name": name},
    )
    for index, (up_to, unit_price) in enumerate(bands):
        await connection.execute(
            text(
                "INSERT INTO price_bands "
                "  (price_list_version_id, band_index, up_to, unit_price_paisa) "
                "VALUES (:version_id, :index, :up_to, :unit_price)"
            ),
            {
                "version_id": version_id,
                "index": index,
                "up_to": up_to,
                "unit_price": unit_price,
            },
        )
    if publish:
        await connection.execute(
            text("UPDATE price_list_versions SET published_at = now() WHERE id = :id"),
            {"id": version_id},
        )
    return version_id


async def make_period(connection: AsyncConnection, customer_id: str) -> str:
    """September 2026 in Asia/Karachi, resolved to UTC exactly as ADR-0009 requires."""
    return await connection.scalar(
        text(
            "INSERT INTO billing_periods "
            "  (customer_id, period_month, period_start, period_end) "
            "VALUES (:customer_id, DATE '2026-09-01', "
            "        TIMESTAMP '2026-09-01 00:00' AT TIME ZONE 'Asia/Karachi', "
            "        TIMESTAMP '2026-10-01 00:00' AT TIME ZONE 'Asia/Karachi') "
            "RETURNING id"
        ),
        {"customer_id": customer_id},
    )


async def make_issued_invoice(
    connection: AsyncConnection, customer_id: str, period_id: str, version_id: str
) -> tuple[str, str]:
    """Draft, one Rs. 15,000 fee line, issued. Returns (invoice_id, line_id)."""
    invoice_id = await connection.scalar(
        text(
            "INSERT INTO invoices (customer_id, billing_period_id, invoice_number, "
            "                      total_paisa) "
            "VALUES (:customer_id, :period_id, :number, 1500000) RETURNING id"
        ),
        {
            "customer_id": customer_id,
            "period_id": period_id,
            "number": f"INV-{uuid.uuid4().hex[:10]}",
        },
    )
    line_id = await connection.scalar(
        text(
            "INSERT INTO invoice_lines (invoice_id, line_number, kind, description, "
            "    quantity, unit_price_paisa, amount_paisa, price_list_version_id) "
            "VALUES (:invoice_id, 1, 'monthly_fee', 'Growth monthly fee', "
            "        1, 1500000, 1500000, :version_id) RETURNING id"
        ),
        {"invoice_id": invoice_id, "version_id": version_id},
    )
    await connection.execute(
        text("UPDATE invoices SET status = 'issued', issued_at = now() WHERE id = :id"),
        {"id": invoice_id},
    )
    return invoice_id, line_id


# ---------------------------------------------------------------------------------------
# ADR-0004: money is bigint paisa, and timestamps are timezone-aware
# ---------------------------------------------------------------------------------------


async def test_every_money_column_is_bigint_and_nothing_is_numeric_or_float(
    conn: AsyncConnection,
) -> None:
    """The money rule, checked against the live catalogue rather than the source.

    Two halves, because either one alone is escapable: every _paisa column must be bigint,
    AND no column anywhere in our tables may be numeric, real, double precision or money.
    A schema that permits a fractional paisa permits a wrong invoice.
    """
    tables = sorted(table.name for table in Base.metadata.sorted_tables)
    rows = (
        await conn.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ANY(:tables)"
            ),
            {"tables": tables},
        )
    ).all()

    assert rows, "expected the schema to be migrated before this test runs"

    money = [row for row in rows if row.column_name.endswith("_paisa")]
    assert money, "expected at least one money column"
    assert [row for row in money if row.data_type != "bigint"] == []

    forbidden = {"numeric", "real", "double precision", "money", "decimal"}
    assert [row for row in rows if row.data_type in forbidden] == []


async def test_every_timestamp_column_is_timezone_aware(conn: AsyncConnection) -> None:
    """ADR-0009 stores UTC. A naive `timestamp` column silently adopts the server's zone,
    which is how five hours of usage lands in the wrong month."""
    tables = sorted(table.name for table in Base.metadata.sorted_tables)
    naive = (
        await conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ANY(:tables) "
                "  AND data_type = 'timestamp without time zone'"
            ),
            {"tables": tables},
        )
    ).all()
    assert naive == []


async def test_customer_billing_timezone_defaults_to_asia_karachi(
    conn: AsyncConnection,
) -> None:
    """ADR-0009 says carry the column from the start even while it has one value."""
    customer_id = await make_customer(conn)
    zone = await conn.scalar(
        text("SELECT billing_timezone FROM customers WHERE id = :id"), {"id": customer_id}
    )
    assert zone == "Asia/Karachi"


# ---------------------------------------------------------------------------------------
# ADR-0013: an issued invoice is immutable, enforced by the database
# ---------------------------------------------------------------------------------------


async def test_an_issued_invoice_cannot_be_updated_or_deleted(conn: AsyncConnection) -> None:
    customer_id = await make_customer(conn)
    version_id = await make_price_version(conn)
    period_id = await make_period(conn, customer_id)
    invoice_id, _ = await make_issued_invoice(conn, customer_id, period_id, version_id)

    message = await refuses(
        conn, "UPDATE invoices SET total_paisa = 1 WHERE id = :id", id=invoice_id
    )
    assert "immutable" in message and "credit note" in message

    await refuses(conn, "DELETE FROM invoices WHERE id = :id", id=invoice_id)

    # Even a no-op UPDATE. Immutable means immutable, not "immutable in the fields we
    # thought of".
    await refuses(conn, "UPDATE invoices SET status = 'issued' WHERE id = :id", id=invoice_id)

    total = await conn.scalar(
        text("SELECT total_paisa FROM invoices WHERE id = :id"), {"id": invoice_id}
    )
    assert total == 1_500_000


async def test_the_lines_of_an_issued_invoice_cannot_be_changed(conn: AsyncConnection) -> None:
    """The amount is on the lines. Freezing the header alone would freeze nothing."""
    customer_id = await make_customer(conn)
    version_id = await make_price_version(conn)
    period_id = await make_period(conn, customer_id)
    invoice_id, line_id = await make_issued_invoice(conn, customer_id, period_id, version_id)

    await refuses(
        conn, "UPDATE invoice_lines SET quantity = 2, amount_paisa = 3000000 WHERE id = :id",
        id=line_id,
    )
    await refuses(conn, "DELETE FROM invoice_lines WHERE id = :id", id=line_id)
    await refuses(
        conn,
        "INSERT INTO invoice_lines (invoice_id, line_number, kind, description, quantity, "
        "  unit_price_paisa, amount_paisa, price_list_version_id) "
        "VALUES (:invoice_id, 2, 'usage', 'sneaked in', 1, 100, 100, :version_id)",
        invoice_id=invoice_id,
        version_id=version_id,
    )


async def test_an_issued_invoice_cannot_be_truncated_away(conn: AsyncConnection) -> None:
    """TRUNCATE bypasses row triggers entirely, so it gets its own statement trigger."""
    message = await refuses(conn, "TRUNCATE invoices CASCADE")
    assert "may not be truncated" in message


async def test_a_draft_invoice_is_still_editable(conn: AsyncConnection) -> None:
    """Immutability starts at issue, not at creation -- otherwise nothing could be built."""
    customer_id = await make_customer(conn)
    version_id = await make_price_version(conn)
    period_id = await make_period(conn, customer_id)
    invoice_id = await conn.scalar(
        text(
            "INSERT INTO invoices (customer_id, billing_period_id, invoice_number) "
            "VALUES (:customer_id, :period_id, :number) RETURNING id"
        ),
        {"customer_id": customer_id, "period_id": period_id, "number": f"D-{uuid.uuid4().hex}"},
    )
    await conn.execute(
        text(
            "INSERT INTO invoice_lines (invoice_id, line_number, kind, description, quantity, "
            "  unit_price_paisa, amount_paisa, price_list_version_id) "
            "VALUES (:invoice_id, 1, 'usage', '100 requests at Rs. 0.50', 100, 50, 5000, "
            "        :version_id)"
        ),
        {"invoice_id": invoice_id, "version_id": version_id},
    )
    await conn.execute(
        text("UPDATE invoices SET total_paisa = 5000 WHERE id = :id"), {"id": invoice_id}
    )
    total = await conn.scalar(
        text("SELECT total_paisa FROM invoices WHERE id = :id"), {"id": invoice_id}
    )
    assert total == 5000


async def test_an_invoice_whose_total_disagrees_with_its_lines_cannot_be_issued(
    conn: AsyncConnection,
) -> None:
    """Issuing is the last moment the number can be checked, so it is when it is checked."""
    customer_id = await make_customer(conn)
    version_id = await make_price_version(conn)
    period_id = await make_period(conn, customer_id)
    invoice_id = await conn.scalar(
        text(
            "INSERT INTO invoices (customer_id, billing_period_id, invoice_number, total_paisa) "
            "VALUES (:customer_id, :period_id, :number, 999999) RETURNING id"
        ),
        {"customer_id": customer_id, "period_id": period_id, "number": f"D-{uuid.uuid4().hex}"},
    )
    await conn.execute(
        text(
            "INSERT INTO invoice_lines (invoice_id, line_number, kind, description, quantity, "
            "  unit_price_paisa, amount_paisa, price_list_version_id) "
            "VALUES (:invoice_id, 1, 'usage', '100 requests at Rs. 0.50', 100, 50, 5000, "
            "        :version_id)"
        ),
        {"invoice_id": invoice_id, "version_id": version_id},
    )
    message = await refuses(
        conn,
        "UPDATE invoices SET status = 'issued', issued_at = now() WHERE id = :id",
        id=invoice_id,
    )
    assert "999999" in message and "5000" in message


async def test_an_empty_invoice_cannot_be_issued(conn: AsyncConnection) -> None:
    customer_id = await make_customer(conn)
    period_id = await make_period(conn, customer_id)
    invoice_id = await conn.scalar(
        text(
            "INSERT INTO invoices (customer_id, billing_period_id, invoice_number) "
            "VALUES (:customer_id, :period_id, :number) RETURNING id"
        ),
        {"customer_id": customer_id, "period_id": period_id, "number": f"E-{uuid.uuid4().hex}"},
    )
    message = await refuses(
        conn,
        "UPDATE invoices SET status = 'issued', issued_at = now() WHERE id = :id",
        id=invoice_id,
    )
    assert "no lines" in message


async def test_an_invoice_line_amount_must_be_its_own_multiplication(
    conn: AsyncConnection,
) -> None:
    """quantity x unit_price = amount, checked on every write. The cheapest possible test
    of "does this line's arithmetic hold?"."""
    customer_id = await make_customer(conn)
    version_id = await make_price_version(conn)
    period_id = await make_period(conn, customer_id)
    invoice_id = await conn.scalar(
        text(
            "INSERT INTO invoices (customer_id, billing_period_id, invoice_number) "
            "VALUES (:customer_id, :period_id, :number) RETURNING id"
        ),
        {"customer_id": customer_id, "period_id": period_id, "number": f"A-{uuid.uuid4().hex}"},
    )
    message = await refuses(
        conn,
        "INSERT INTO invoice_lines (invoice_id, line_number, kind, description, quantity, "
        "  unit_price_paisa, amount_paisa, price_list_version_id) "
        "VALUES (:invoice_id, 1, 'usage', '100 at Rs. 0.50', 100, 50, 5001, :version_id)",
        invoice_id=invoice_id,
        version_id=version_id,
    )
    assert "ck_invoice_lines_amount_is_product" in message


async def test_a_prior_period_line_must_name_the_period_it_came_from(
    conn: AsyncConnection,
) -> None:
    """ADR-0010 roll-forward: a labelled prior-period line is useless without the label."""
    customer_id = await make_customer(conn)
    version_id = await make_price_version(conn)
    period_id = await make_period(conn, customer_id)
    invoice_id = await conn.scalar(
        text(
            "INSERT INTO invoices (customer_id, billing_period_id, invoice_number) "
            "VALUES (:customer_id, :period_id, :number) RETURNING id"
        ),
        {"customer_id": customer_id, "period_id": period_id, "number": f"P-{uuid.uuid4().hex}"},
    )
    message = await refuses(
        conn,
        "INSERT INTO invoice_lines (invoice_id, line_number, kind, description, quantity, "
        "  unit_price_paisa, amount_paisa, price_list_version_id) "
        "VALUES (:invoice_id, 1, 'prior_period_usage', '12,400 requests from August', "
        "        12400, 35, 434000, :version_id)",
        invoice_id=invoice_id,
        version_id=version_id,
    )
    assert "ck_invoice_lines_prior_period_has_period" in message


# ---------------------------------------------------------------------------------------
# ADR-0005: a price list version is immutable once referenced
# ---------------------------------------------------------------------------------------


async def test_a_published_price_list_version_cannot_be_changed(conn: AsyncConnection) -> None:
    """The day someone "just fixes a typo" in a band price is the day a past invoice stops
    being reproducible."""
    version_id = await make_price_version(conn)

    message = await refuses(
        conn,
        "UPDATE price_list_versions SET monthly_fee_paisa = 1 WHERE id = :id",
        id=version_id,
    )
    assert "published and immutable" in message

    await refuses(conn, "DELETE FROM price_list_versions WHERE id = :id", id=version_id)
    await refuses(
        conn,
        "UPDATE price_bands SET unit_price_paisa = 1 WHERE price_list_version_id = :id",
        id=version_id,
    )
    await refuses(conn, "DELETE FROM price_bands WHERE price_list_version_id = :id", id=version_id)
    await refuses(
        conn,
        "INSERT INTO price_bands (price_list_version_id, band_index, up_to, unit_price_paisa) "
        "VALUES (:id, 9, 9999, 1)",
        id=version_id,
    )


async def test_an_unpublished_draft_cannot_be_referenced_by_anything(
    conn: AsyncConnection,
) -> None:
    """The other half of "immutable once referenced". Without this, the freeze-on-publish
    rule has a hole: reference a draft, then edit it."""
    customer_id = await make_customer(conn)
    draft_id = await make_price_version(conn, publish=False)

    message = await refuses(
        conn,
        "INSERT INTO plan_assignments (customer_id, price_list_version_id, effective) "
        "VALUES (:customer_id, :version_id, tstzrange(now(), NULL, '[)'))",
        customer_id=customer_id,
        version_id=draft_id,
    )
    assert "unpublished draft" in message


async def test_a_draft_price_list_version_is_editable_until_it_is_published(
    conn: AsyncConnection,
) -> None:
    version_id = await make_price_version(conn, publish=False)
    await conn.execute(
        text("UPDATE price_list_versions SET monthly_fee_paisa = 9000000 WHERE id = :id"),
        {"id": version_id},
    )
    fee = await conn.scalar(
        text("SELECT monthly_fee_paisa FROM price_list_versions WHERE id = :id"),
        {"id": version_id},
    )
    assert fee == 9_000_000


async def test_a_band_ladder_is_validated_at_the_moment_it_is_published(
    conn: AsyncConnection,
) -> None:
    """Publishing is the last moment the ladder can be fixed and the first moment it can be
    checked as a whole. These are the same rules meter.domain.plans.PriceList enforces, so
    a version the database accepts is one the rating function accepts."""
    unbounded_not_last = await make_price_version(
        conn, publish=False, bands=((None, 35), (500_000, 50))
    )
    message = await refuses(
        conn,
        "UPDATE price_list_versions SET published_at = now() WHERE id = :id",
        id=unbounded_not_last,
    )
    assert "only the final band" in message

    all_bounded = await make_price_version(
        conn, publish=False, bands=((500_000, 50), (1_000_000, 35))
    )
    message = await refuses(
        conn, "UPDATE price_list_versions SET published_at = now() WHERE id = :id", id=all_bounded
    )
    assert "must be unbounded" in message

    not_increasing = await make_price_version(
        conn, publish=False, bands=((500_000, 50), (500_000, 35), (None, 25))
    )
    message = await refuses(
        conn,
        "UPDATE price_list_versions SET published_at = now() WHERE id = :id",
        id=not_increasing,
    )
    assert "strictly increase" in message

    no_bands = await make_price_version(conn, publish=False, bands=())
    message = await refuses(
        conn, "UPDATE price_list_versions SET published_at = now() WHERE id = :id", id=no_bands
    )
    assert "at least one band" in message


async def test_a_negative_band_price_is_refused(conn: AsyncConnection) -> None:
    """ADR-0008's spending-limit inversion needs the ladder monotonic in quantity, and says
    a negative marginal band should be forbidden at the price list level. This is that."""
    version_id = await make_price_version(conn, publish=False, bands=())
    message = await refuses(
        conn,
        "INSERT INTO price_bands (price_list_version_id, band_index, up_to, unit_price_paisa) "
        "VALUES (:id, 0, NULL, -1)",
        id=version_id,
    )
    assert "ck_price_bands_unit_price_non_negative" in message


# ---------------------------------------------------------------------------------------
# ADR-0006: a customer's plan timeline resolves to exactly one segment at any instant
# ---------------------------------------------------------------------------------------


async def test_a_customer_cannot_hold_two_overlapping_plan_assignments(
    conn: AsyncConnection,
) -> None:
    """"Which plan was this customer on at instant T?" must return at most one row. A
    database that can return two will eventually price a request twice, or not at all."""
    customer_id = await make_customer(conn)
    growth = await make_price_version(conn, name="Growth")
    scale = await make_price_version(conn, name="Scale")
    start = datetime(2026, 9, 1, tzinfo=UTC)

    await conn.execute(
        text(
            "INSERT INTO plan_assignments (customer_id, price_list_version_id, effective) "
            "VALUES (:customer_id, :version_id, tstzrange(:lo, :hi, '[)'))"
        ),
        {
            "customer_id": customer_id,
            "version_id": growth,
            "lo": start,
            "hi": start + timedelta(days=17),
        },
    )
    message = await refuses(
        conn,
        "INSERT INTO plan_assignments (customer_id, price_list_version_id, effective) "
        "VALUES (:customer_id, :version_id, tstzrange(:lo, NULL, '[)'))",
        customer_id=customer_id,
        version_id=scale,
        lo=start + timedelta(days=16),
    )
    assert "ex_plan_assignments_no_overlap" in message


async def test_abutting_plan_assignments_are_allowed_and_resolve_to_one_segment(
    conn: AsyncConnection,
) -> None:
    """Half-open [from, to) is what makes the changeover instant belong to exactly one
    plan. ADR-0006 says the change day belongs to the NEW plan; this is the shape that
    lets that be true without a tie."""
    customer_id = await make_customer(conn)
    growth = await make_price_version(conn, name="Growth")
    scale = await make_price_version(conn, name="Scale")
    start = datetime(2026, 9, 1, tzinfo=UTC)
    change = start + timedelta(days=17)

    for version_id, lo, hi in ((growth, start, change), (scale, change, None)):
        await conn.execute(
            text(
                "INSERT INTO plan_assignments (customer_id, price_list_version_id, effective) "
                "VALUES (:customer_id, :version_id, tstzrange(:lo, :hi, '[)'))"
            ),
            {"customer_id": customer_id, "version_id": version_id, "lo": lo, "hi": hi},
        )

    resolved = (
        await conn.execute(
            text(
                "SELECT price_list_version_id FROM plan_assignments "
                # The CAST is load-bearing under asyncpg: without it the driver resolves
                # the right-hand side of @> to a range rather than an instant. Repositories
                # issuing this query must cast too.
                "WHERE customer_id = :customer_id AND effective @> CAST(:instant AS timestamptz)"
            ),
            {"customer_id": customer_id, "instant": change},
        )
    ).scalars().all()
    assert resolved == [scale]


async def test_a_plan_assignment_must_be_half_open(conn: AsyncConnection) -> None:
    customer_id = await make_customer(conn)
    version_id = await make_price_version(conn)
    message = await refuses(
        conn,
        "INSERT INTO plan_assignments (customer_id, price_list_version_id, effective) "
        "VALUES (:customer_id, :version_id, tstzrange(:lo, :hi, '[]'))",
        customer_id=customer_id,
        version_id=version_id,
        lo=datetime(2026, 9, 1, tzinfo=UTC),
        hi=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert "ck_plan_assignments_effective_is_half_open" in message


# ---------------------------------------------------------------------------------------
# ADR-0015: API keys
# ---------------------------------------------------------------------------------------


async def test_a_key_that_is_not_a_hex_digest_is_refused(conn: AsyncConnection) -> None:
    """The schema refuses to store a plaintext key, which is what a bug would write."""
    customer_id = await make_customer(conn)
    message = await refuses(
        conn,
        "INSERT INTO api_keys (customer_id, key_hash, prefix) "
        "VALUES (:customer_id, 'sk_live_the_actual_secret', 'sk_live')",
        customer_id=customer_id,
    )
    assert "ck_api_keys_hash_is_sha256_hex" in message


async def test_a_customer_may_hold_several_keys_and_revocation_keeps_the_row(
    conn: AsyncConnection,
) -> None:
    customer_id = await make_customer(conn)
    first = await make_api_key(conn, customer_id)
    second = await make_api_key(conn, customer_id)

    await conn.execute(
        text("UPDATE api_keys SET revoked_at = now() WHERE id = :id"), {"id": first}
    )
    live = (
        await conn.execute(
            text(
                "SELECT id FROM api_keys WHERE customer_id = :customer_id AND revoked_at IS NULL"
            ),
            {"customer_id": customer_id},
        )
    ).scalars().all()
    assert live == [second]

    # The revoked key is still there, so a charge attributed to it is still traceable.
    still_present = await conn.scalar(
        text("SELECT count(*) FROM api_keys WHERE customer_id = :customer_id"),
        {"customer_id": customer_id},
    )
    assert still_present == 2


async def test_two_customers_cannot_share_a_key_hash(conn: AsyncConnection) -> None:
    first = await make_customer(conn, "First")
    second = await make_customer(conn, "Second")
    await conn.execute(
        text(
            "INSERT INTO api_keys (customer_id, key_hash, prefix) "
            "VALUES (:customer_id, :key_hash, 'mk_test')"
        ),
        {"customer_id": first, "key_hash": SHA256_OF_NOTHING},
    )
    message = await refuses(
        conn,
        "INSERT INTO api_keys (customer_id, key_hash, prefix) "
        "VALUES (:customer_id, :key_hash, 'mk_test')",
        customer_id=second,
        key_hash=SHA256_OF_NOTHING,
    )
    assert "uq_api_keys_key_hash" in message


# ---------------------------------------------------------------------------------------
# ADR-0007 / ADR-0016: usage events, idempotency, partitioning
# ---------------------------------------------------------------------------------------


async def insert_usage(
    connection: AsyncConnection,
    *,
    customer_id: str,
    api_key_id: str,
    occurred_at: datetime,
    period_start: datetime,
    idempotency_key: str,
    outcome: str = "success",
    status_code: int = 200,
    billable: bool = True,
) -> int:
    """The capture write, exactly as the hot path will issue it."""
    return await connection.scalar(
        text(
            "INSERT INTO usage_events (billing_period_start, customer_id, api_key_id, "
            "    occurred_at, status_code, outcome, billable, idempotency_key) "
            "VALUES (:period_start, :customer_id, :api_key_id, :occurred_at, :status_code, "
            "        CAST(:outcome AS usage_outcome), :billable, :idempotency_key) "
            "ON CONFLICT (idempotency_key, billing_period_start) DO NOTHING "
            "RETURNING id"
        ),
        {
            "period_start": period_start,
            "customer_id": customer_id,
            "api_key_id": api_key_id,
            "occurred_at": occurred_at,
            "status_code": status_code,
            "outcome": outcome,
            "billable": billable,
            "idempotency_key": idempotency_key,
        },
    )


async def test_a_retried_capture_cannot_double_count(conn: AsyncConnection) -> None:
    """The invariant this whole table exists to protect. A retry after an ambiguous failure
    must land on the same row, not a second one."""
    customer_id = await make_customer(conn)
    api_key_id = await make_api_key(conn, customer_id)
    period_start = datetime(2026, 8, 31, 19, 0, tzinfo=UTC)  # 2026-09-01 00:00 Asia/Karachi
    occurred_at = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
    key = f"evt-{uuid.uuid4()}"

    first = await insert_usage(
        conn,
        customer_id=customer_id,
        api_key_id=api_key_id,
        occurred_at=occurred_at,
        period_start=period_start,
        idempotency_key=key,
    )
    retry = await insert_usage(
        conn,
        customer_id=customer_id,
        api_key_id=api_key_id,
        occurred_at=occurred_at,
        period_start=period_start,
        idempotency_key=key,
    )
    assert first is not None
    assert retry is None  # ON CONFLICT DO NOTHING: the row was already durable

    count = await conn.scalar(
        text("SELECT count(*) FROM usage_events WHERE idempotency_key = :key"), {"key": key}
    )
    assert count == 1


async def test_usage_rows_land_in_the_partition_for_their_billing_month(
    conn: AsyncConnection,
) -> None:
    """The partition boundary is the Asia/Karachi month start expressed in UTC -- 19:00 on
    the last day of the previous UTC month. One second either side is a different invoice."""
    customer_id = await make_customer(conn)
    api_key_id = await make_api_key(conn, customer_id)
    september = datetime(2026, 8, 31, 19, 0, tzinfo=UTC)
    october = datetime(2026, 9, 30, 19, 0, tzinfo=UTC)

    for period_start in (september, october):
        await insert_usage(
            conn,
            customer_id=customer_id,
            api_key_id=api_key_id,
            occurred_at=period_start + timedelta(hours=1),
            period_start=period_start,
            idempotency_key=f"evt-{uuid.uuid4()}",
        )

    placement = (
        await conn.execute(
            text(
                "SELECT tableoid::regclass::text AS partition, count(*) "
                "  FROM usage_events WHERE customer_id = :customer_id "
                " GROUP BY 1 ORDER BY 1"
            ),
            {"customer_id": customer_id},
        )
    ).all()
    assert [row.partition for row in placement] == ["usage_events_2026_09", "usage_events_2026_10"]


async def test_the_default_partition_catches_a_period_nobody_created_a_partition_for(
    conn: AsyncConnection,
) -> None:
    """On this table a failed INSERT is a lost billable request, so a row with nowhere to go
    must still land somewhere. It must also be loud: a non-empty default partition is an
    alert, not a resting place."""
    customer_id = await make_customer(conn)
    api_key_id = await make_api_key(conn, customer_id)
    far_future = datetime(2099, 12, 31, 19, 0, tzinfo=UTC)

    await insert_usage(
        conn,
        customer_id=customer_id,
        api_key_id=api_key_id,
        occurred_at=far_future,
        period_start=far_future,
        idempotency_key=f"evt-{uuid.uuid4()}",
    )
    partition = await conn.scalar(
        text(
            "SELECT tableoid::regclass::text FROM usage_events WHERE customer_id = :customer_id"
        ),
        {"customer_id": customer_id},
    )
    assert partition == "usage_events_default"


async def test_every_outcome_in_adr_0007_is_storable_with_its_billability(
    conn: AsyncConnection,
) -> None:
    """ADR-0007's five rows. `billable` stores the decision made at capture time rather than
    deriving it, so a later change to the rule cannot retroactively re-bill history."""
    customer_id = await make_customer(conn)
    api_key_id = await make_api_key(conn, customer_id)
    period_start = datetime(2026, 8, 31, 19, 0, tzinfo=UTC)
    cases = (
        ("success", 200, True),
        ("client_error", 400, True),
        ("unauthenticated", 401, False),
        ("server_error", 500, False),
        ("limit_refused", 429, False),
    )
    for outcome, status_code, billable in cases:
        await insert_usage(
            conn,
            customer_id=customer_id,
            api_key_id=api_key_id,
            occurred_at=datetime(2026, 9, 16, 9, 0, tzinfo=UTC),
            period_start=period_start,
            idempotency_key=f"evt-{uuid.uuid4()}",
            outcome=outcome,
            status_code=status_code,
            billable=billable,
        )
    billed = await conn.scalar(
        text(
            "SELECT count(*) FROM usage_events "
            "WHERE customer_id = :customer_id AND billable"
        ),
        {"customer_id": customer_id},
    )
    assert billed == 2


async def test_creating_a_partition_is_idempotent_and_uses_karachi_boundaries(
    conn: AsyncConnection,
) -> None:
    """How partitions get created going forward: one idempotent function a scheduler can
    call blindly, deriving its bounds from the billing timezone rather than from UTC."""
    name = await conn.scalar(text("SELECT meter_create_usage_partition(DATE '2031-03-14')"))
    assert name == "usage_events_2031_03"
    again = await conn.scalar(text("SELECT meter_create_usage_partition(DATE '2031-03-01')"))
    assert again == "usage_events_2031_03"

    bounds = await conn.scalar(
        text(
            "SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_class c "
            "WHERE c.relname = 'usage_events_2031_03'"
        )
    )
    # 2031-03-01 00:00 Asia/Karachi is 2031-02-28 19:00 UTC.
    assert "2031-02-28 19:00:00+00" in bounds
    assert "2031-03-31 19:00:00+00" in bounds


# ---------------------------------------------------------------------------------------
# ADR-0016 / ADR-0010: rollups keep the grain, and carry roll-forward state
# ---------------------------------------------------------------------------------------


async def test_the_rollup_grain_includes_the_api_key(conn: AsyncConnection) -> None:
    """ADR-0016: "the grain should be chosen generously now". Two keys on the same day,
    same plan, same version are two rows -- which is what keeps "which of my keys caused
    March?" answerable in April, after the per-request rows are gone."""
    customer_id = await make_customer(conn)
    first_key = await make_api_key(conn, customer_id)
    second_key = await make_api_key(conn, customer_id)
    version_id = await make_price_version(conn)
    period_id = await make_period(conn, customer_id)
    assignment_id = await conn.scalar(
        text(
            "INSERT INTO plan_assignments (customer_id, price_list_version_id, effective) "
            "VALUES (:customer_id, :version_id, tstzrange(:lo, NULL, '[)')) RETURNING id"
        ),
        {
            "customer_id": customer_id,
            "version_id": version_id,
            "lo": datetime(2026, 8, 31, 19, 0, tzinfo=UTC),
        },
    )

    upsert = (
        "INSERT INTO usage_rollups (customer_id, billing_period_id, usage_date, "
        "    price_list_version_id, plan_assignment_id, api_key_id, billable_requests) "
        "VALUES (:customer_id, :period_id, DATE '2026-09-16', :version_id, :assignment_id, "
        "        :api_key_id, :count) "
        "ON CONFLICT (customer_id, usage_date, plan_assignment_id, price_list_version_id, "
        "             api_key_id) "
        "DO UPDATE SET billable_requests = EXCLUDED.billable_requests"
    )
    common = {
        "customer_id": customer_id,
        "period_id": period_id,
        "version_id": version_id,
        "assignment_id": assignment_id,
    }
    await conn.execute(text(upsert), {**common, "api_key_id": first_key, "count": 100})
    await conn.execute(text(upsert), {**common, "api_key_id": second_key, "count": 900})
    # Re-running the aggregation pass must land on the same row, not a third one.
    await conn.execute(text(upsert), {**common, "api_key_id": first_key, "count": 150})

    rows = (
        await conn.execute(
            text(
                "SELECT api_key_id, billable_requests FROM usage_rollups "
                "WHERE customer_id = :customer_id ORDER BY billable_requests"
            ),
            {"customer_id": customer_id},
        )
    ).all()
    assert [row.billable_requests for row in rows] == [150, 900]


async def test_an_unbilled_rollup_is_findable_which_is_how_roll_forward_works(
    conn: AsyncConnection,
) -> None:
    """ADR-0010: late usage for a closed period rolls forward. In the schema that is just
    a rollup with no invoice_id -- which is also the query that answers "is any revenue
    sitting unbilled right now?"."""
    customer_id = await make_customer(conn)
    api_key_id = await make_api_key(conn, customer_id)
    version_id = await make_price_version(conn)
    period_id = await make_period(conn, customer_id)
    assignment_id = await conn.scalar(
        text(
            "INSERT INTO plan_assignments (customer_id, price_list_version_id, effective) "
            "VALUES (:customer_id, :version_id, tstzrange(:lo, NULL, '[)')) RETURNING id"
        ),
        {
            "customer_id": customer_id,
            "version_id": version_id,
            "lo": datetime(2026, 8, 31, 19, 0, tzinfo=UTC),
        },
    )
    await conn.execute(
        text(
            "INSERT INTO usage_rollups (customer_id, billing_period_id, usage_date, "
            "    price_list_version_id, plan_assignment_id, api_key_id, billable_requests) "
            "VALUES (:customer_id, :period_id, DATE '2026-09-16', :version_id, "
            "        :assignment_id, :api_key_id, 12400)"
        ),
        {
            "customer_id": customer_id,
            "period_id": period_id,
            "version_id": version_id,
            "assignment_id": assignment_id,
            "api_key_id": api_key_id,
        },
    )
    await conn.execute(
        text(
            "UPDATE billing_periods SET status = 'closed', closed_at = now() WHERE id = :id"
        ),
        {"id": period_id},
    )

    unbilled = await conn.scalar(
        text(
            "SELECT sum(billable_requests) FROM usage_rollups "
            "WHERE customer_id = :customer_id AND invoice_id IS NULL"
        ),
        {"customer_id": customer_id},
    )
    assert unbilled == 12400


async def test_a_closed_period_still_accepts_the_usage_it_incurred(
    conn: AsyncConnection,
) -> None:
    """ADR-0010: late usage is recorded against the period it was incurred in and excluded
    from the closed total -- excluded by a query, never by refusing the write. Refusing it
    would violate "no request goes unbilled" outright."""
    customer_id = await make_customer(conn)
    api_key_id = await make_api_key(conn, customer_id)
    period_id = await make_period(conn, customer_id)
    await conn.execute(
        text("UPDATE billing_periods SET status = 'closed', closed_at = now() WHERE id = :id"),
        {"id": period_id},
    )
    row_id = await insert_usage(
        conn,
        customer_id=customer_id,
        api_key_id=api_key_id,
        occurred_at=datetime(2026, 9, 30, 18, 59, 59, tzinfo=UTC),
        period_start=datetime(2026, 8, 31, 19, 0, tzinfo=UTC),
        idempotency_key=f"late-{uuid.uuid4()}",
    )
    assert row_id is not None


# ---------------------------------------------------------------------------------------
# ADR-0009 / ADR-0012: periods and limits
# ---------------------------------------------------------------------------------------


async def test_a_billing_period_stores_the_resolved_karachi_boundary_in_utc(
    conn: AsyncConnection,
) -> None:
    customer_id = await make_customer(conn)
    period_id = await make_period(conn, customer_id)
    row = (
        await conn.execute(
            text("SELECT period_start, period_end FROM billing_periods WHERE id = :id"),
            {"id": period_id},
        )
    ).one()
    assert row.period_start == datetime(2026, 8, 31, 19, 0, tzinfo=UTC)
    assert row.period_end == datetime(2026, 9, 30, 19, 0, tzinfo=UTC)


async def test_a_customer_gets_one_period_per_month_and_one_invoice_per_period(
    conn: AsyncConnection,
) -> None:
    customer_id = await make_customer(conn)
    period_id = await make_period(conn, customer_id)

    message = await refuses(
        conn,
        "INSERT INTO billing_periods (customer_id, period_month, period_start, period_end) "
        "VALUES (:customer_id, DATE '2026-09-01', now(), now() + interval '1 day')",
        customer_id=customer_id,
    )
    assert "uq_billing_periods_customer_month" in message

    await conn.execute(
        text(
            "INSERT INTO invoices (customer_id, billing_period_id, invoice_number) "
            "VALUES (:customer_id, :period_id, :number)"
        ),
        {"customer_id": customer_id, "period_id": period_id, "number": f"N-{uuid.uuid4().hex}"},
    )
    message = await refuses(
        conn,
        "INSERT INTO invoices (customer_id, billing_period_id, invoice_number) "
        "VALUES (:customer_id, :period_id, :number)",
        customer_id=customer_id,
        period_id=period_id,
        number=f"N-{uuid.uuid4().hex}",
    )
    assert "uq_invoices_customer_period" in message


async def test_a_spending_limit_records_what_its_threshold_was_computed_from(
    conn: AsyncConnection,
) -> None:
    """ADR-0008 calls a missed threshold recomputation "the worst kind" of failure, because
    everything looks fine. Recording the inputs is what makes staleness detectable."""
    customer_id = await make_customer(conn)
    version_id = await make_price_version(conn)
    period_id = await make_period(conn, customer_id)

    limit_id = await conn.scalar(
        text(
            "INSERT INTO spending_limits (customer_id, billing_period_id, limit_paisa) "
            "VALUES (:customer_id, :period_id, 5000000) RETURNING id"
        ),
        {"customer_id": customer_id, "period_id": period_id},
    )
    pending = await conn.scalar(
        text(
            "SELECT count(*) FROM spending_limits "
            "WHERE billing_period_id = :period_id AND threshold_requests IS NULL"
        ),
        {"period_id": period_id},
    )
    assert pending == 1

    # A threshold without the timestamp saying when it was computed is refused: it would be
    # indistinguishable from a fresh one.
    message = await refuses(
        conn, "UPDATE spending_limits SET threshold_requests = 7000000 WHERE id = :id", id=limit_id
    )
    assert "ck_spending_limits_threshold_has_timestamp" in message

    await conn.execute(
        text(
            "UPDATE spending_limits SET threshold_requests = 7000000, "
            "  threshold_computed_at = now(), threshold_price_list_version_id = :version_id "
            "WHERE id = :id"
        ),
        {"id": limit_id, "version_id": version_id},
    )
    row = (
        await conn.execute(
            text(
                "SELECT threshold_requests, threshold_price_list_version_id, updated_at "
                "FROM spending_limits WHERE id = :id"
            ),
            {"id": limit_id},
        )
    ).one()
    assert row.threshold_requests == 7_000_000
    assert row.threshold_price_list_version_id == version_id
