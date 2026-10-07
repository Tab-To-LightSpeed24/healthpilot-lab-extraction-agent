"""widen observation text columns

A real report's reference range (a multi-line interpretation block) exceeded
VARCHAR(128) and Postgres rejected the whole page insert.

Revision ID: c4e8f1a3d6b7
Revises: b3d7a1c9e5f2
Create Date: 2026-10-08 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4e8f1a3d6b7'
down_revision: Union[str, None] = 'b3d7a1c9e5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# column -> (old type, new type)
CHANGES = {
    "value": (sa.String(128), sa.String(512)),
    "unit": (sa.String(64), sa.String(128)),
    "reference_range": (sa.String(128), sa.Text()),
    "specimen": (sa.String(128), sa.String(256)),
    "method": (sa.String(256), sa.String(512)),
    "timing": (sa.String(128), sa.String(256)),
    "flag": (sa.String(32), sa.String(64)),
    "suggested_value": (sa.String(128), sa.String(256)),
}


def upgrade() -> None:
    # batch_alter_table so this also runs on SQLite (no ALTER COLUMN there).
    with op.batch_alter_table("observations") as batch:
        for name, (old, new) in CHANGES.items():
            batch.alter_column(name, existing_type=old, type_=new, existing_nullable=True)


def downgrade() -> None:
    # Narrowing can fail if long values exist; clip them first.
    for name, (old, new) in CHANGES.items():
        limit = getattr(old, "length", None)
        if limit:
            op.execute(sa.text(f"UPDATE observations SET {name} = substr({name}, 1, {limit}) WHERE length({name}) > {limit}"))
    with op.batch_alter_table("observations") as batch:
        for name, (old, new) in CHANGES.items():
            batch.alter_column(name, existing_type=new, type_=old, existing_nullable=True)
