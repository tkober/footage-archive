"""add DirectoryStats table (recursive directory status, #139)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'DirectoryStats',
        sa.Column('directory', sa.Text(), nullable=False),
        sa.Column('parent', sa.Text(), nullable=False),
        sa.Column('media_files', sa.Integer(), nullable=True),
        sa.Column('tracked_files', sa.Integer(), nullable=True),
        sa.Column('subtree_media_files', sa.Integer(), nullable=True),
        sa.Column('subtree_tracked_files', sa.Integer(), nullable=True),
        sa.Column('subtree_complete', sa.Boolean(), nullable=False),
        sa.Column('walked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('source', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('directory'),
    )
    op.create_index('idx__DirectoryStats__parent', 'DirectoryStats', ['parent'])


def downgrade() -> None:
    op.drop_index('idx__DirectoryStats__parent', table_name='DirectoryStats')
    op.drop_table('DirectoryStats')
