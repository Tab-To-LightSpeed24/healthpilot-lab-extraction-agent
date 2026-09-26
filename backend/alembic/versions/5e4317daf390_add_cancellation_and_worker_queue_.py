"""add cancellation and worker queue fields to documents

Revision ID: 5e4317daf390
Revises: 302898ee8a21
Create Date: 2026-09-27 03:04:11.710370

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e4317daf390'
down_revision: Union[str, None] = '302898ee8a21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('updated_at', sa.DateTime(), nullable=True))
    # NOT NULL with no server_default fails outright on Postgres against a
    # table that may already have rows (the same class of mistake caught and
    # fixed earlier today in the common_test_rank migration).
    op.add_column(
        'documents',
        sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Postgres backs sa.Enum with a real CREATE TYPE ... enum, and adding a
    # new value to an existing Postgres enum type is not something
    # autogenerate detects or CREATE/ALTER TABLE can do -- it needs its own
    # explicit ALTER TYPE. SQLite has no real enum type (the column just
    # stores the string), so there's nothing to do there.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE documentstatus ADD VALUE IF NOT EXISTS 'cancelled'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE; downgrading the enum itself
    # is intentionally not supported here, only the added columns are.
    op.drop_column('documents', 'cancel_requested')
    op.drop_column('documents', 'updated_at')
