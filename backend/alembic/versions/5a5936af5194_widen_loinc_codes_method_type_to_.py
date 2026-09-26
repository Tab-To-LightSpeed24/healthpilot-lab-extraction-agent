"""widen loinc_codes.method_type to accommodate real LOINC data

Revision ID: 5a5936af5194
Revises: 5e4317daf390
Create Date: 2026-09-27 04:10:23.606651

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5a5936af5194'
down_revision: Union[str, None] = '5e4317daf390'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table (not a bare op.alter_column) so this also works on
    # SQLite, which has no ALTER COLUMN and needs Alembic's table-rebuild
    # strategy -- a bare op.alter_column(type_=...) works on Postgres but
    # fails outright on SQLite ("near ALTER: syntax error"), which every
    # local dev/test run uses.
    with op.batch_alter_table("loinc_codes") as batch_op:
        batch_op.alter_column(
            "method_type",
            existing_type=sa.VARCHAR(length=128),
            type_=sa.String(length=256),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("loinc_codes") as batch_op:
        batch_op.alter_column(
            "method_type",
            existing_type=sa.String(length=256),
            type_=sa.VARCHAR(length=128),
            existing_nullable=True,
        )
