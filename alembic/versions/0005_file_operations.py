"""add FileOperations journal table

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'FileOperations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('source_path', sa.Text(), nullable=False),
        sa.Column('target_path', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('FileOperations')
