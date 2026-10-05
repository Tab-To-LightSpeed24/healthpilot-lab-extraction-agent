"""add extraction provenance (fallback/manual) fields

Revision ID: 7b1d4c9e2a10
Revises: 5a5936af5194
Create Date: 2026-10-02 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7b1d4c9e2a10'
down_revision: Union[str, None] = '5a5936af5194'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL columns carry a server_default so this applies cleanly to a
    # table that already has rows (existing data was all LLM-extracted).
    op.add_column('observations', sa.Column('extraction_source', sa.String(length=16), nullable=False, server_default='llm'))
    op.add_column('observations', sa.Column('is_edited', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('documents', sa.Column('used_fallback', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('documents', sa.Column('fallback_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('documents', 'fallback_reason')
    op.drop_column('documents', 'used_fallback')
    op.drop_column('observations', 'is_edited')
    op.drop_column('observations', 'extraction_source')
