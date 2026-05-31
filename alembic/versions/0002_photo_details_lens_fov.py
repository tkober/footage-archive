"""add lens / 35mm-equivalent / field-of-view columns to PhotoDetails

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-31

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('PhotoDetails', sa.Column('lens', sa.Text(), nullable=True))
    op.add_column('PhotoDetails', sa.Column('focal_length_35mm', sa.Float(), nullable=True))
    op.add_column('PhotoDetails', sa.Column('scale_factor_35mm', sa.Float(), nullable=True))
    op.add_column('PhotoDetails', sa.Column('field_of_view', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('PhotoDetails', 'field_of_view')
    op.drop_column('PhotoDetails', 'scale_factor_35mm')
    op.drop_column('PhotoDetails', 'focal_length_35mm')
    op.drop_column('PhotoDetails', 'lens')
