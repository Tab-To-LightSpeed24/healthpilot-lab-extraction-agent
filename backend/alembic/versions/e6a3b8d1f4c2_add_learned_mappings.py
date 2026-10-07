"""add learned_mappings

Revision ID: e6a3b8d1f4c2
Revises: d5f2a9c7e1b3
Create Date: 2026-10-08 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e6a3b8d1f4c2'
down_revision: Union[str, None] = 'd5f2a9c7e1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'learned_mappings',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('key', sa.String(512), nullable=False),
        sa.Column('loinc_num', sa.String(32), nullable=False),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_learned_mappings_key', 'learned_mappings', ['key'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_learned_mappings_key', table_name='learned_mappings')
    op.drop_table('learned_mappings')
