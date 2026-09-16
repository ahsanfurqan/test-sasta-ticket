"""Invoices and their lines. Write-once by construction, and by trigger.

An issued invoice is immutable (ADR-0013) and the database enforces it, so there is no
`update_invoice` in this module and there never should be. The only transitions available
are: create a draft, add lines to a draft, issue it, or delete a draft that was never
issued. Regeneration that disagrees with an issued invoice raises; it does not "fix".

The invoice NUMBER is deterministic -- derived from the customer and the period, never from
a sequence. That is what turns "regeneration must produce the same number or fail loudly"
into something the unique constraint enforces for us: a second attempt to issue the same
customer-month collides on `uq_invoices_number` and on `uq_invoices_customer_period`,
rather than quietly creating a second invoice Finance has to reconcile by hand.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

USAGE_LINE_KINDS = ("usage", "prior_period_usage")


@dataclass(frozen=True, slots=True)
class InvoiceRow:
    id: str
    customer_id: str
    billing_period_id: str
    invoice_number: str
    status: str
    total_paisa: int
    issued_at: datetime | None
    #: Present only on the customer-facing reads, which join the period to name the month.
    period_month: date | None = None


@dataclass(frozen=True, slots=True)
class LineRow:
    line_number: int
    kind: str
    description: str
    quantity: int
    unit_price_paisa: int
    amount_paisa: int
    price_list_version_id: str
    plan_assignment_id: str | None = None
    band_index: int | None = None
    usage_period_id: str | None = None


class InvoiceImmutable(RuntimeError):
    """A regeneration disagreed with an issued invoice. Never resolved by writing."""


def invoice_number(customer_id: str, period_month: date) -> str:
    """Deterministic, sortable, and unique per customer-month by construction."""
    return f"INV-{period_month:%Y%m}-{customer_id.replace('-', '')[:12].upper()}"


def _invoice_row(row) -> InvoiceRow:
    return InvoiceRow(
        # asyncpg returns uuid.UUID; the rest of the system speaks str ids.
        id=str(row.id),
        customer_id=str(row.customer_id),
        billing_period_id=str(row.billing_period_id),
        invoice_number=row.invoice_number,
        status=row.status,
        total_paisa=int(row.total_paisa),
        issued_at=row.issued_at,
        period_month=getattr(row, "period_month", None),
    )


_COLUMNS = "id, customer_id, billing_period_id, invoice_number, status, total_paisa, issued_at"


async def get_for_period(
    conn: AsyncConnection, customer_id: str, period_id: str
) -> InvoiceRow | None:
    row = (
        await conn.execute(
            text(
                f"SELECT {_COLUMNS} FROM invoices "
                " WHERE customer_id = :customer_id AND billing_period_id = :period_id"
            ),
            {"customer_id": customer_id, "period_id": period_id},
        )
    ).one_or_none()
    return None if row is None else _invoice_row(row)


async def delete_draft(conn: AsyncConnection, invoice_id: str) -> None:
    """Remove a draft and its lines. The trigger refuses this for an issued invoice, which
    is the intended behaviour and not something to work around."""
    await conn.execute(
        text("DELETE FROM invoice_lines WHERE invoice_id = :id"), {"id": invoice_id}
    )
    await conn.execute(text("DELETE FROM invoices WHERE id = :id"), {"id": invoice_id})


async def create_draft(
    conn: AsyncConnection, customer_id: str, period_id: str, number: str
) -> str:
    return str(
        await conn.scalar(
            text(
                "INSERT INTO invoices (customer_id, billing_period_id, invoice_number, "
                "                      status, total_paisa) "
                "VALUES (:customer_id, :period_id, :number, 'draft', 0) RETURNING id"
            ),
            {"customer_id": customer_id, "period_id": period_id, "number": number},
        )
    )


async def add_lines(conn: AsyncConnection, invoice_id: str, lines: list[LineRow]) -> None:
    for line in lines:
        await conn.execute(
            text(
                """
                INSERT INTO invoice_lines (
                    invoice_id, line_number, kind, description, quantity,
                    unit_price_paisa, amount_paisa, price_list_version_id,
                    plan_assignment_id, band_index, usage_period_id
                ) VALUES (
                    :invoice_id, :line_number, :kind, :description, :quantity,
                    :unit_price_paisa, :amount_paisa, :price_list_version_id,
                    :plan_assignment_id, :band_index, :usage_period_id
                )
                """
            ),
            {
                "invoice_id": invoice_id,
                "line_number": line.line_number,
                "kind": line.kind,
                "description": line.description,
                "quantity": line.quantity,
                "unit_price_paisa": line.unit_price_paisa,
                "amount_paisa": line.amount_paisa,
                "price_list_version_id": line.price_list_version_id,
                "plan_assignment_id": line.plan_assignment_id,
                "band_index": line.band_index,
                "usage_period_id": line.usage_period_id,
            },
        )


async def issue(conn: AsyncConnection, invoice_id: str, total_paisa: int) -> None:
    """Draft -> issued. The trigger re-checks that the lines sum to this total, at the one
    moment the number stops being changeable -- so a total that disagrees with its lines
    can never be issued, whatever the application believes."""
    await conn.execute(
        text(
            "UPDATE invoices SET status = 'issued', total_paisa = :total, issued_at = now() "
            " WHERE id = :id"
        ),
        {"id": invoice_id, "total": total_paisa},
    )


_LIST_FOR_CUSTOMER = text(
    """
    SELECT i.id::text AS id, i.customer_id::text AS customer_id,
           i.billing_period_id::text AS billing_period_id, i.invoice_number,
           i.status, i.total_paisa, i.issued_at, bp.period_month
      FROM invoices i
      JOIN billing_periods bp ON bp.id = i.billing_period_id
     WHERE i.customer_id = :customer_id
     ORDER BY bp.period_month DESC
    """
)
_BY_NUMBER = text(
    """
    SELECT i.id::text AS id, i.customer_id::text AS customer_id,
           i.billing_period_id::text AS billing_period_id, i.invoice_number,
           i.status, i.total_paisa, i.issued_at, bp.period_month
      FROM invoices i
      JOIN billing_periods bp ON bp.id = i.billing_period_id
     WHERE i.invoice_number = :invoice_number
    """
)


async def list_for_customer(conn: AsyncConnection, customer_id: str) -> list[InvoiceRow]:
    """Every invoice this customer has, newest period first."""
    rows = (await conn.execute(_LIST_FOR_CUSTOMER, {"customer_id": customer_id})).all()
    return [_invoice_row(row) for row in rows]


async def get_by_number(conn: AsyncConnection, invoice_number: str) -> InvoiceRow | None:
    """One invoice by its customer-visible number."""
    row = (await conn.execute(_BY_NUMBER, {"invoice_number": invoice_number})).first()
    return _invoice_row(row) if row else None


async def lines_for(conn: AsyncConnection, invoice_id: str) -> list[LineRow]:
    rows = (
        await conn.execute(
            text(
                "SELECT line_number, kind, description, quantity, unit_price_paisa, "
                "       amount_paisa, price_list_version_id, plan_assignment_id, "
                "       band_index, usage_period_id "
                "  FROM invoice_lines WHERE invoice_id = :id ORDER BY line_number"
            ),
            {"id": invoice_id},
        )
    ).all()
    return [
        LineRow(
            line_number=row.line_number,
            kind=row.kind,
            description=row.description,
            quantity=int(row.quantity),
            unit_price_paisa=int(row.unit_price_paisa),
            amount_paisa=int(row.amount_paisa),
            price_list_version_id=str(row.price_list_version_id),
            plan_assignment_id=(
                None if row.plan_assignment_id is None else str(row.plan_assignment_id)
            ),
            band_index=row.band_index,
            usage_period_id=(
                None if row.usage_period_id is None else str(row.usage_period_id)
            ),
        )
        for row in rows
    ]


BILLED_BY_SEGMENT_SQL = """
SELECT il.plan_assignment_id AS plan_assignment_id,
       COALESCE(sum(il.quantity), 0) AS billed
  FROM invoice_lines il
  JOIN invoices i ON i.id = il.invoice_id
 WHERE i.customer_id = :customer_id
   AND i.status = 'issued'
   AND il.kind IN ('usage', 'prior_period_usage')
   -- A current-period usage line has no usage_period_id (the CHECK forbids it), so the
   -- period it belongs to is its invoice's. A prior-period line names its own.
   AND COALESCE(il.usage_period_id, i.billing_period_id) = :period_id
 GROUP BY il.plan_assignment_id
"""


async def billed_quantity_by_segment(
    conn: AsyncConnection, customer_id: str, period_id: str
) -> dict[str, int]:
    """How many requests of one period have ALREADY been billed, per plan segment.

    This is the left-hand side of ADR-0010's roll-forward. It is computed from invoice
    lines rather than from a flag on the rollup, because invoice lines are immutable: the
    answer to "what did we already charge for this period?" cannot drift, however much
    late usage arrives afterwards.
    """
    rows = (
        await conn.execute(
            text(BILLED_BY_SEGMENT_SQL),
            {"customer_id": customer_id, "period_id": period_id},
        )
    ).all()
    return {
        str(row.plan_assignment_id): int(row.billed)
        for row in rows
        if row.plan_assignment_id is not None
    }
