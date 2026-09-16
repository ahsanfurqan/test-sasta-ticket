"""ORM models -- the source of schema truth. Owned by data-model. See docs/adr/0003.

Empty of tables by design: the schema is next session's work, and the plan/price tables
are blocked on open question #7 (whether custom pricing is an override on a plan or a
price list of its own). YAGNI -- a model written before that question is settled is a
model written twice.

House rules for every model added here:

  * Money columns are BigInteger paisa. Never Numeric, never Float, never Money.
    See docs/adr/0004-money-as-integer-paisa.md.
  * Timestamps are timezone-aware (DateTime(timezone=True)) and stored UTC.
  * Anything on a write path carries an idempotency key, so a retry after an ambiguous
    failure cannot double-count.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base. Alembic autogenerate targets Base.metadata."""
