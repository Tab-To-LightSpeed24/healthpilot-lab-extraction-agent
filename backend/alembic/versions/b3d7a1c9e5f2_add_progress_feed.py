"""add live progress feed to documents

Revision ID: b3d7a1c9e5f2
Revises: 9c2e5f7a1b34
Create Date: 2026-10-04 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3d7a1c9e5f2'
down_revision: Union[str, None] = '9c2e5f7a1b34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('progress', sa.JSON(), nullable=True))
    op.add_column('documents', sa.Column('pages_done', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('documents', 'pages_done')
    op.drop_column('documents', 'progress')
