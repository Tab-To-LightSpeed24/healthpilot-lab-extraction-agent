"""add processing_seconds to documents

Revision ID: d5f2a9c7e1b3
Revises: c4e8f1a3d6b7
Create Date: 2026-10-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5f2a9c7e1b3'
down_revision: Union[str, None] = 'c4e8f1a3d6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('processing_seconds', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('documents', 'processing_seconds')
