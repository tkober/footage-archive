"""add Files.size_bytes/mtime_ns for the incremental scan skip rule

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Both NULL for every existing row (#136) — a NULL signature is never
    # treated as "unchanged" by the skip rule, so every currently-tracked
    # file is still hashed+probed on its next scan, which then fills these in.
    op.add_column('Files', sa.Column('size_bytes', sa.BigInteger(), nullable=True))
    op.add_column('Files', sa.Column('mtime_ns', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('Files', 'mtime_ns')
    op.drop_column('Files', 'size_bytes')
