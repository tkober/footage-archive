"""add Lists / ListItems tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'Lists',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    op.create_table(
        'ListItems',
        sa.Column('list_id', sa.Integer(), nullable=False),
        sa.Column('md5_hash', sa.String(), nullable=False),
        sa.Column('item_code', sa.String(length=8), nullable=False),
        sa.Column('added_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['list_id'], ['Lists.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['md5_hash'], ['Files.md5_hash']),
        sa.PrimaryKeyConstraint('list_id', 'md5_hash'),
        sa.UniqueConstraint('list_id', 'item_code', name='uq__ListItems__list_id_item_code'),
    )
    op.create_index('idx__ListItems__md5_hash', 'ListItems', ['md5_hash'])


def downgrade() -> None:
    op.drop_index('idx__ListItems__md5_hash', table_name='ListItems')
    op.drop_table('ListItems')
    op.drop_table('Lists')
