"""add validation notes and suggested value to observations

Revision ID: 9c2e5f7a1b34
Revises: 7b1d4c9e2a10
Create Date: 2026-10-03 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9c2e5f7a1b34'
down_revision: Union[str, None] = '7b1d4c9e2a10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('observations', sa.Column('validation_notes', sa.JSON(), nullable=True))
    op.add_column('observations', sa.Column('suggested_value', sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column('observations', 'suggested_value')
    op.drop_column('observations', 'validation_notes')
