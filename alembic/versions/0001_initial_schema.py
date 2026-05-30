"""initial schema

Revision ID: 0001
Revises:
Create Date: 2025-05-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'Locations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.Text(), nullable=True),
        sa.Column('city', sa.Text(), nullable=True),
        sa.Column('region', sa.Text(), nullable=True),
        sa.Column('country', sa.Text(), nullable=True),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx__Locations__country', 'Locations', ['country'])
    op.create_index('idx__Locations__city', 'Locations', ['city'])
    op.create_index('idx__Locations__country_region_city', 'Locations', ['country', 'region', 'city'])

    op.create_table(
        'Files',
        sa.Column('md5_hash', sa.String(), nullable=False),
        sa.Column('file_name', sa.Text(), nullable=True),
        sa.Column('file_extension', sa.Text(), nullable=True),
        sa.Column('media_type', sa.Text(), nullable=True),
        sa.Column('directory', sa.Text(), nullable=True),
        sa.Column('last_indexed_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('md5_hash'),
    )
    op.create_index('idx__Files__directory', 'Files', ['directory'])

    op.create_table(
        'FileDetails',
        sa.Column('md5_hash', sa.String(), nullable=False),
        sa.Column('location_id', sa.Integer(), nullable=True),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('recorded_at', sa.Text(), nullable=True),
        sa.Column('last_modified_at', sa.Text(), nullable=True),
        sa.Column('json', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['location_id'], ['Locations.id']),
        sa.PrimaryKeyConstraint('md5_hash'),
    )

    op.create_table(
        'VideoDetails',
        sa.Column('md5_hash', sa.String(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('frame_rate', sa.Float(), nullable=True),
        sa.Column('frame_rate_verbose', sa.Text(), nullable=True),
        sa.Column('video_codec', sa.Text(), nullable=True),
        sa.Column('bit_depth', sa.Integer(), nullable=True),
        sa.Column('audio_codec', sa.Text(), nullable=True),
        sa.Column('audio_bit_depth', sa.Integer(), nullable=True),
        sa.Column('audio_sample_rate', sa.Integer(), nullable=True),
        sa.Column('audio_channels', sa.Integer(), nullable=True),
        sa.Column('duration_tc', sa.Text(), nullable=True),
        sa.Column('shot', sa.Text(), nullable=True),
        sa.Column('scene', sa.Text(), nullable=True),
        sa.Column('take', sa.Text(), nullable=True),
        sa.Column('angle', sa.Text(), nullable=True),
        sa.Column('move', sa.Text(), nullable=True),
        sa.Column('shot_type', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('md5_hash'),
    )

    op.create_table(
        'PhotoDetails',
        sa.Column('md5_hash', sa.String(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('camera_make', sa.Text(), nullable=True),
        sa.Column('camera_model', sa.Text(), nullable=True),
        sa.Column('iso', sa.Integer(), nullable=True),
        sa.Column('aperture', sa.Float(), nullable=True),
        sa.Column('shutter_speed', sa.Text(), nullable=True),
        sa.Column('focal_length', sa.Float(), nullable=True),
        sa.Column('color_space', sa.Text(), nullable=True),
        sa.Column('bit_depth', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('md5_hash'),
    )

    op.create_table(
        'Keywords',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('keyword', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('keyword'),
    )
    op.create_index('idx__Keywords__keyword', 'Keywords', ['keyword'])

    op.create_table(
        'FileKeywords',
        sa.Column('md5_hash', sa.String(), nullable=False),
        sa.Column('keyword_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['md5_hash'], ['Files.md5_hash']),
        sa.ForeignKeyConstraint(['keyword_id'], ['Keywords.id']),
        sa.PrimaryKeyConstraint('md5_hash', 'keyword_id'),
    )

    op.create_table(
        'ClipPreviews',
        sa.Column('md5_hash', sa.String(), nullable=False),
        sa.Column('frames', sa.Integer(), nullable=True),
        sa.Column('frame_height', sa.Integer(), nullable=True),
        sa.Column('frame_width', sa.Integer(), nullable=True),
        sa.Column('padding', sa.Integer(), nullable=True),
        sa.Column('overall_height', sa.Integer(), nullable=True),
        sa.Column('overall_width', sa.Integer(), nullable=True),
        sa.Column('data', sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint('md5_hash'),
    )


def downgrade() -> None:
    op.drop_table('ClipPreviews')
    op.drop_table('FileKeywords')
    op.drop_index('idx__Keywords__keyword', table_name='Keywords')
    op.drop_table('Keywords')
    op.drop_table('PhotoDetails')
    op.drop_table('VideoDetails')
    op.drop_table('FileDetails')
    op.drop_index('idx__Files__directory', table_name='Files')
    op.drop_table('Files')
    op.drop_index('idx__Locations__country_region_city', table_name='Locations')
    op.drop_index('idx__Locations__city', table_name='Locations')
    op.drop_index('idx__Locations__country', table_name='Locations')
    op.drop_table('Locations')
