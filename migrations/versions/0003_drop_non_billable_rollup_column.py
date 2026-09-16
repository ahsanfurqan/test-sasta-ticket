"""drop usage_rollups.non_billable_requests

Revision ID: 0003_drop_non_billable
Revises: 0002_schema
Create Date: 2026-09-16

Owner: data-model
Why: the column was unreachable by design and always read 0, which misleads anyone who
     queries it. The hot path deliberately does NOT stream non-billable outcomes into the
     usage stream -- at the stream's fail-closed bound, a client hammering refused requests
     could otherwise displace usage we are owed money for (ADR-0007, ADR-0018). They are
     counted in a per-period Redis hash instead. So no non-billable row ever reaches
     usage_events, and the aggregation can only ever write zero here. Verified against
     2,481,162 recorded events: every one of them billable.

     If non-billable usage is ever wanted durably, the DESIGN decision comes first -- stream
     them, or drain the Redis hash -- and the column comes back with it. Keeping an empty
     column against that possibility is storing a guess.

Locking: ACCESS EXCLUSIVE on usage_rollups for the duration. DROP COLUMN in Postgres is a
     catalogue-only operation (the column is marked dropped, not rewritten), so it is fast
     regardless of row count -- but it still queues behind and blocks every reader and writer
     of the table while it waits for the lock. usage_rollups is written by the aggregation
     pass, not by the request path, so the blast radius is a paused aggregation rather than
     refused customer traffic. Run it between passes; set a short lock_timeout and retry
     rather than letting it queue behind a long transaction.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003_drop_non_billable"
down_revision: str | None = "0002_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The check constraint names the column, so it goes first.
    op.drop_constraint(
        "ck_usage_rollups_counts_non_negative", "usage_rollups", type_="check"
    )
    op.drop_column("usage_rollups", "non_billable_requests")
    op.create_check_constraint(
        "ck_usage_rollups_counts_non_negative", "usage_rollups", "billable_requests >= 0"
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_usage_rollups_counts_non_negative", "usage_rollups", type_="check"
    )
    op.add_column(
        "usage_rollups",
        sa.Column(
            "non_billable_requests",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_usage_rollups_counts_non_negative",
        "usage_rollups",
        "billable_requests >= 0 AND non_billable_requests >= 0",
    )
