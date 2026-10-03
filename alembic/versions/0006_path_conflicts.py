"""add PathConflicts table

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'PathConflicts',
        sa.Column('md5_hash', sa.String(), nullable=False),
        sa.Column('candidate_path', sa.Text(), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('found_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['md5_hash'], ['Files.md5_hash'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('md5_hash', 'candidate_path'),
    )


def downgrade() -> None:
    op.drop_table('PathConflicts')
