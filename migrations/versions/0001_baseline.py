"""baseline: extensions only

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-16

Owner: data-model
Why: proves the migration path runs end to end, and installs the extensions the schema
     will need. No tables yet -- the schema is next session's work, and it is blocked on
     open question #7 (the shape of custom pricing) before the plan/price tables can be
     finalised.
Locking: none. CREATE EXTENSION IF NOT EXISTS on an empty database.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # gen_random_uuid() for surrogate keys, and digest() for hashing API keys --
    # keys are stored hashed, never in plaintext.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
