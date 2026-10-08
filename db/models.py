from sqlalchemy import (
    BigInteger, Column, DateTime, Float, ForeignKey, Index, Integer, LargeBinary,
    MetaData, String, Table, Text, UniqueConstraint,
)
from sqlalchemy.sql import func

metadata = MetaData()

locations_table = Table(
    'Locations', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('name', Text),
    Column('city', Text),
    Column('region', Text),
    Column('country', Text),
    Column('latitude', Float),
    Column('longitude', Float),
)

files_table = Table(
    'Files', metadata,
    Column('md5_hash', String, primary_key=True),
    Column('file_name', Text),
    Column('file_extension', Text),
    Column('media_type', Text),
    Column('directory', Text),
    Column('last_indexed_at', DateTime, server_default=func.now()),
    # Incremental scan skip rule (#136): the file's size + mtime as of its
    # last successful probe, written only once that probe finishes (never by
    # the pre-probe insert_scan_results upsert, see Database.set_file_signatures) —
    # NULL until then, and never treated as "unchanged" while NULL.
    Column('size_bytes', BigInteger),
    Column('mtime_ns', BigInteger),
)

file_details_table = Table(
    'FileDetails', metadata,
    Column('md5_hash', String, primary_key=True),
    Column('location_id', Integer, ForeignKey('Locations.id')),
    Column('latitude', Float),
    Column('longitude', Float),
    Column('altitude', Float),
    Column('description', Text),
    Column('recorded_at', Text),
    Column('last_modified_at', Text),
    Column('json', Text),
)

video_details_table = Table(
    'VideoDetails', metadata,
    Column('md5_hash', String, primary_key=True),
    Column('width', Integer),
    Column('height', Integer),
    Column('frame_rate', Float),
    Column('frame_rate_verbose', Text),
    Column('video_codec', Text),
    Column('bit_depth', Integer),
    Column('audio_codec', Text),
    Column('audio_bit_depth', Integer),
    Column('audio_sample_rate', Integer),
    Column('audio_channels', Integer),
    Column('duration_tc', Text),
    Column('shot', Text),
    Column('scene', Text),
    Column('take', Text),
    Column('angle', Text),
    Column('move', Text),
    Column('shot_type', Text),
)

photo_details_table = Table(
    'PhotoDetails', metadata,
    Column('md5_hash', String, primary_key=True),
    Column('width', Integer),
    Column('height', Integer),
    Column('camera_make', Text),
    Column('camera_model', Text),
    Column('iso', Integer),
    Column('aperture', Float),
    Column('shutter_speed', Text),
    Column('focal_length', Float),
    Column('color_space', Text),
    Column('bit_depth', Integer),
    Column('lens', Text),
    Column('focal_length_35mm', Float),
    Column('scale_factor_35mm', Float),
    Column('field_of_view', Float),
)

keywords_table = Table(
    'Keywords', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('keyword', Text, nullable=False, unique=True),
)

file_keywords_table = Table(
    'FileKeywords', metadata,
    Column('md5_hash', String, ForeignKey('Files.md5_hash'), primary_key=True),
    Column('keyword_id', Integer, ForeignKey('Keywords.id'), primary_key=True),
)

clip_previews_table = Table(
    'ClipPreviews', metadata,
    Column('md5_hash', String, primary_key=True),
    Column('frames', Integer),
    Column('frame_height', Integer),
    Column('frame_width', Integer),
    Column('padding', Integer),
    Column('overall_height', Integer),
    Column('overall_width', Integer),
    Column('data', LargeBinary),
)

lists_table = Table(
    'Lists', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('name', Text, nullable=False, unique=True),
    Column('created_at', DateTime, server_default=func.now()),
)

list_items_table = Table(
    'ListItems', metadata,
    Column('list_id', Integer, ForeignKey('Lists.id', ondelete='CASCADE'), primary_key=True),
    Column('md5_hash', String, ForeignKey('Files.md5_hash'), primary_key=True),
    Column('item_code', String(8), nullable=False),
    Column('added_at', DateTime, server_default=func.now()),
    UniqueConstraint('list_id', 'item_code', name='uq__ListItems__list_id_item_code'),
)

file_operations_table = Table(
    'FileOperations', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('kind', Text, nullable=False),
    Column('source_path', Text, nullable=False),
    Column('target_path', Text, nullable=False),
    Column('status', Text, nullable=False),
    Column('error', Text),
    Column('created_at', DateTime, server_default=func.now()),
    Column('finished_at', DateTime),
)

path_conflicts_table = Table(
    'PathConflicts', metadata,
    Column('md5_hash', String, ForeignKey('Files.md5_hash', ondelete='CASCADE'), primary_key=True),
    Column('candidate_path', Text, primary_key=True),
    Column('source', Text, nullable=False),
    Column('found_at', DateTime, server_default=func.now()),
)

preview_status_table = Table(
    'PreviewStatus', metadata,
    Column('md5_hash', String, primary_key=True),
    Column('status', String, nullable=False),
    Column('reason', String),
    Column('attempted_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

Index('idx__Locations__country', locations_table.c.country)
Index('idx__Locations__city', locations_table.c.city)
Index('idx__Locations__country_region_city',
      locations_table.c.country, locations_table.c.region, locations_table.c.city)
Index('idx__Files__directory', files_table.c.directory)
Index('idx__Keywords__keyword', keywords_table.c.keyword)
Index('idx__ListItems__md5_hash', list_items_table.c.md5_hash)
# PathConflicts needs no extra index: its PK (md5_hash, candidate_path) already
# covers lookups by md5_hash.
