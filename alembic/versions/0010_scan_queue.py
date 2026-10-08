"""add ScanJobs/ScanUnits tables (persistent scan queue, #137)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ScanJobs',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('root_path', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('options', JSONB(), server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'ScanUnits',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('job_id', sa.Text(), nullable=False),
        sa.Column('directory', sa.Text(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('media_file_count', sa.Integer(), nullable=True),
        sa.Column('tracked_file_count', sa.Integer(), nullable=True),
        sa.Column('progress', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('result', JSONB(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['job_id'], ['ScanJobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx__ScanUnits__job_id_position', 'ScanUnits', ['job_id', 'position'])
    # Partial unique index: a directory is never QUEUED/RUNNING in more than
    # one unit at a time, across every job.
    op.create_index(
        'uq__ScanUnits__directory_active', 'ScanUnits', ['directory'],
        unique=True, postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index('uq__ScanUnits__directory_active', table_name='ScanUnits')
    op.drop_index('idx__ScanUnits__job_id_position', table_name='ScanUnits')
    op.drop_table('ScanUnits')
    op.drop_table('ScanJobs')
