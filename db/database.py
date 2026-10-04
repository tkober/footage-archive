import logging
import uuid
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from sqlalchemy import case, delete, func, select, tuple_, update
from sqlalchemy.exc import IntegrityError

from db.engine import get_engine, upsert, upsert_ignore
from db.list_codes import generate_item_code, normalize_item_code
from db.models import (
    clip_previews_table,
    file_details_table,
    file_keywords_table,
    file_operations_table,
    files_table,
    keywords_table,
    list_items_table,
    lists_table,
    locations_table,
    path_conflicts_table,
    photo_details_table,
    video_details_table,
)
from ffmpeg.ffmpeg import ClipPreview
from scanner.scanner import ScanResult

logger = logging.getLogger(__name__)


class DuplicateListNameError(Exception):
    """Raised when creating/renaming a list to a name that already exists."""


class StaleConflictError(Exception):
    """Raised by resolve_path_conflict() when the tracked path changed
    since the caller read it, so the guarded repoint matched no row."""


class UndoRenameFailedError(Exception):
    """Raised by run_guarded_rename() when the transaction commit failed
    AND the subsequent attempt to physically reverse the rename
    (undo_rename) also failed. The filesystem and DB may now be
    inconsistent — callers must NOT mark the FileOperations journal row as
    'rolled_back' or 'failed' in this case; leave it 'pending' so
    recover_pending_operations() can reconcile it on next startup."""

    def __init__(self, commit_error: Exception, undo_error: Exception):
        self.commit_error = commit_error
        self.undo_error = undo_error
        super().__init__(
            f'Transaction commit failed ({commit_error!r}) and reversing the '
            f'physical rename also failed ({undo_error!r}) — filesystem and '
            f'DB may be inconsistent; left for startup recovery.'
        )


def generate_identifier():
    return str(uuid.uuid4()).replace('-', '')


def _df_to_records(df: pd.DataFrame, table) -> list[dict] | None:
    """Filter DataFrame columns to those present in the table; require md5_hash."""
    table_cols = {c.name for c in table.columns}
    available = [c for c in df.columns if c in table_cols]
    if not available or 'md5_hash' not in available:
        return None
    # Byte-identical files (e.g. macOS '._*' AppleDouble sidecars) share an MD5;
    # Postgres rejects ON CONFLICT DO UPDATE when one statement hits a row twice.
    subset = df[available].drop_duplicates(subset='md5_hash', keep='last')
    subset = subset.where(pd.notna(subset), None)
    return subset.to_dict(orient='records')


class Database:
    # ------------------------------------------------------------------
    # Insert / upsert
    # ------------------------------------------------------------------

    def insert_scan_results(self, scan_results: list[ScanResult],
                            identifier: str = generate_identifier()) -> None:
        df = pd.DataFrame([r.model_dump() for r in scan_results])
        records = _df_to_records(df, files_table)
        if records:
            with get_engine().begin() as conn:
                conn.execute(upsert(files_table, records, ['md5_hash']))

    def insert_file_details(self, details: pd.DataFrame,
                            identifier: str = generate_identifier()) -> None:
        records = _df_to_records(details, file_details_table)
        if records:
            with get_engine().begin() as conn:
                conn.execute(upsert(file_details_table, records, ['md5_hash']))

    def insert_video_details(self, details: pd.DataFrame,
                             identifier: str = generate_identifier()) -> None:
        records = _df_to_records(details, video_details_table)
        if records:
            with get_engine().begin() as conn:
                conn.execute(upsert(video_details_table, records, ['md5_hash']))

    def insert_photo_details(self, details: pd.DataFrame,
                             identifier: str = generate_identifier()) -> None:
        records = _df_to_records(details, photo_details_table)
        if records:
            with get_engine().begin() as conn:
                conn.execute(upsert(photo_details_table, records, ['md5_hash']))

    def insert_keywords(self, keywords: pd.DataFrame,
                        identifier: str = generate_identifier()) -> None:
        with get_engine().begin() as conn:
            for _, row in keywords[['md5_hash', 'keyword']].iterrows():
                conn.execute(
                    upsert_ignore(keywords_table, [{'keyword': row['keyword']}], ['keyword'])
                )
                kw_id = conn.execute(
                    select(keywords_table.c.id).where(keywords_table.c.keyword == row['keyword'])
                ).scalar()
                conn.execute(
                    upsert_ignore(
                        file_keywords_table,
                        [{'md5_hash': row['md5_hash'], 'keyword_id': kw_id}],
                        ['md5_hash', 'keyword_id'],
                    )
                )

    def insert_raw_preview(self, md5_hash: str, data: bytes,
                           identifier: str = generate_identifier()) -> None:
        with get_engine().begin() as conn:
            conn.execute(
                upsert(clip_previews_table, [{'md5_hash': md5_hash, 'data': data}], ['md5_hash'])
            )

    def insert_clip_preview(self, clip_preview: ClipPreview,
                            identifier: str = generate_identifier()) -> None:
        with get_engine().begin() as conn:
            conn.execute(upsert(clip_previews_table, [clip_preview.model_dump()], ['md5_hash']))

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_tracked_files_in_directory(self, directory: str) -> dict:
        stmt = (
            select(
                files_table.c.file_name, files_table.c.md5_hash, files_table.c.media_type,
                video_details_table.c.duration_tc,
            )
            .select_from(
                files_table.outerjoin(
                    video_details_table,
                    files_table.c.md5_hash == video_details_table.c.md5_hash,
                )
            )
            .where(files_table.c.directory == directory)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {
            row[0]: {'md5_hash': row[1], 'media_type': row[2], 'duration_tc': row[3]}
            for row in rows
        }

    def get_file_by_hash(self, md5_hash: str) -> Optional[dict]:
        stmt = select(files_table).where(files_table.c.md5_hash == md5_hash)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).fetchone()
        return row._asdict() if row is not None else None

    def get_file_by_path(self, file_path: str) -> Optional[dict]:
        p = Path(file_path)
        stmt = (
            select(files_table)
            .where(files_table.c.directory == str(p.parent),
                   files_table.c.file_name == p.name)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).fetchone()
        return row._asdict() if row is not None else None

    def get_video_details(self, md5_hash: str) -> Optional[dict]:
        stmt = (
            select(
                video_details_table.c.width, video_details_table.c.height,
                video_details_table.c.frame_rate, video_details_table.c.frame_rate_verbose,
                video_details_table.c.video_codec, video_details_table.c.bit_depth,
                video_details_table.c.audio_codec, video_details_table.c.audio_bit_depth,
                video_details_table.c.audio_sample_rate, video_details_table.c.audio_channels,
                video_details_table.c.duration_tc,
            )
            .where(video_details_table.c.md5_hash == md5_hash)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).fetchone()
        return row._asdict() if row is not None else None

    def get_photo_details(self, md5_hash: str) -> Optional[dict]:
        stmt = (
            select(
                photo_details_table.c.width, photo_details_table.c.height,
                photo_details_table.c.camera_make, photo_details_table.c.camera_model,
                photo_details_table.c.iso, photo_details_table.c.aperture,
                photo_details_table.c.shutter_speed, photo_details_table.c.focal_length,
                photo_details_table.c.color_space, photo_details_table.c.bit_depth,
                photo_details_table.c.lens, photo_details_table.c.focal_length_35mm,
                photo_details_table.c.scale_factor_35mm, photo_details_table.c.field_of_view,
            )
            .where(photo_details_table.c.md5_hash == md5_hash)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).fetchone()
        return row._asdict() if row is not None else None

    def rename_file(self, md5_hash: str, new_file_name: str) -> None:
        stmt = (
            update(files_table)
            .where(files_table.c.md5_hash == md5_hash)
            .values(file_name=new_file_name)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def get_keywords(self, md5_hash: str) -> list[str]:
        stmt = (
            select(keywords_table.c.keyword)
            .join(file_keywords_table, keywords_table.c.id == file_keywords_table.c.keyword_id)
            .where(file_keywords_table.c.md5_hash == md5_hash)
            .order_by(keywords_table.c.keyword)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [row[0] for row in rows]

    def add_keyword(self, md5_hash: str, keyword: str) -> None:
        with get_engine().begin() as conn:
            conn.execute(upsert_ignore(keywords_table, [{'keyword': keyword}], ['keyword']))
            kw_id = conn.execute(
                select(keywords_table.c.id).where(keywords_table.c.keyword == keyword)
            ).scalar()
            conn.execute(
                upsert_ignore(
                    file_keywords_table,
                    [{'md5_hash': md5_hash, 'keyword_id': kw_id}],
                    ['md5_hash', 'keyword_id'],
                )
            )

    def delete_keyword(self, md5_hash: str, keyword: str) -> None:
        kw_subq = (
            select(keywords_table.c.id)
            .where(keywords_table.c.keyword == keyword)
            .scalar_subquery()
        )
        stmt = (
            delete(file_keywords_table)
            .where(file_keywords_table.c.md5_hash == md5_hash,
                   file_keywords_table.c.keyword_id == kw_subq)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def get_file_gps(self, md5_hash: str) -> tuple[float, float, float | None] | None:
        stmt = (
            select(file_details_table.c.latitude, file_details_table.c.longitude,
                   file_details_table.c.altitude)
            .where(file_details_table.c.md5_hash == md5_hash)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).fetchone()
        if row and row[0] is not None and row[1] is not None:
            return (row[0], row[1], row[2])
        return None

    @staticmethod
    def _cluster_cell_for_zoom(zoom: int) -> float:
        # Grid cell size (degrees), halving every zoom level so each zoom-in step
        # refines the clusters (≈1/10th of the visible span at that zoom). Clustering
        # runs at *every* zoom — no special high-zoom path — so co-located files
        # (e.g. many photos sharing one named location's coordinates) collapse into a
        # single selectable cluster instead of stacking invisibly. At max zoom (~22)
        # the cell is a few metres, so only truly co-located files still group.
        return 200.0 / (2 ** max(zoom, 0))

    def get_map_points(self, west: float, south: float, east: float, north: float,
                       zoom: int) -> list[dict]:
        coalesce_lat = func.coalesce(locations_table.c.latitude, file_details_table.c.latitude)
        coalesce_lon = func.coalesce(locations_table.c.longitude, file_details_table.c.longitude)

        base_stmt = (
            select(
                files_table.c.md5_hash,
                files_table.c.file_name,
                files_table.c.directory,
                files_table.c.media_type,
                coalesce_lat.label('lat'),
                coalesce_lon.label('lon'),
            )
            .select_from(
                files_table
                .join(file_details_table, files_table.c.md5_hash == file_details_table.c.md5_hash)
                .outerjoin(locations_table, file_details_table.c.location_id == locations_table.c.id)
            )
            .where(
                coalesce_lat.isnot(None),
                coalesce_lon.isnot(None),
                coalesce_lat.between(south, north),
                coalesce_lon.between(west, east),
            )
        )

        cell = self._cluster_cell_for_zoom(zoom)
        subq = base_stmt.subquery()
        # Bucket points into a grid cell for grouping, but position each cluster
        # marker at the *centroid* (avg) of its members rather than the rounded
        # grid node — otherwise a cluster snaps to a grid coordinate that can sit
        # far from the actual data (e.g. out in the ocean).
        lat_cell = func.round(subq.c.lat / cell) * cell
        lon_cell = func.round(subq.c.lon / cell) * cell
        is_video_expr = subq.c.media_type.in_(['video', '360_video'])

        cluster_stmt = (
            select(
                func.avg(subq.c.lat).label('latitude'),
                func.avg(subq.c.lon).label('longitude'),
                func.count().label('count'),
                func.sum(case((is_video_expr, 1), else_=0)).label('video_count'),
                func.sum(case((~is_video_expr, 1), else_=0)).label('photo_count'),
                # For a single-file cluster these min()s are that file's values
                # (used for the preview + "open details" link); ignored otherwise.
                func.min(subq.c.md5_hash).label('md5_hash'),
                func.min(subq.c.file_name).label('file_name'),
                func.min(subq.c.directory).label('directory'),
                func.min(subq.c.media_type).label('media_type'),
                # Member bounding box — the "open in search" link filters to it.
                func.min(subq.c.lat).label('bbox_south'),
                func.max(subq.c.lat).label('bbox_north'),
                func.min(subq.c.lon).label('bbox_west'),
                func.max(subq.c.lon).label('bbox_east'),
                # Per-member details so a small all-stills leaf can show each photo
                # inline (kept only for those clusters — see trim below — so the
                # payload stays small for large clusters).
                func.json_agg(
                    func.json_build_object(
                        'md5_hash', subq.c.md5_hash,
                        'file_name', subq.c.file_name,
                        'directory', subq.c.directory,
                        'media_type', subq.c.media_type,
                    )
                ).label('members'),
            )
            .select_from(subq)
            .group_by(lat_cell, lon_cell)
        )

        with get_engine().connect() as conn:
            rows = conn.execute(cluster_stmt).fetchall()
        result = []
        for row in rows:
            r = row._asdict()
            # Only expose member lists for small, all-stills clusters (the map
            # renders their thumbnails); drop otherwise to keep responses lean.
            if not (1 < r['count'] < 5 and r['video_count'] == 0):
                r['members'] = None
            result.append(r)
        return result

    _FACET_COLS = {
        'camera_make':  photo_details_table.c.camera_make,
        'camera_model': photo_details_table.c.camera_model,
        'video_codec':  video_details_table.c.video_codec,
    }

    def get_facet_values(self, field: str, q: str, limit: int) -> list[str]:
        if field == 'country':
            col = locations_table.c.country
            stmt = (
                select(col.distinct())
                .join(file_details_table,
                      locations_table.c.id == file_details_table.c.location_id)
                .where(col.isnot(None), col.ilike(f'%{q}%'))
                .order_by(col)
                .limit(limit)
            )
        elif field in self._FACET_COLS:
            col = self._FACET_COLS[field]
            stmt = (
                select(col.distinct())
                .where(col.isnot(None), col.ilike(f'%{q}%'))
                .order_by(col)
                .limit(limit)
            )
        else:
            return []
        with get_engine().connect() as conn:
            return [r[0] for r in conn.execute(stmt).fetchall()]

    def search_files(self, query: dict) -> tuple[int, list[dict]]:
        conditions = []

        if query.get('media_types'):
            conditions.append(files_table.c.media_type.in_(query['media_types']))

        if query.get('keywords'):
            kw_subq = (
                select(file_keywords_table.c.md5_hash)
                .join(keywords_table,
                      file_keywords_table.c.keyword_id == keywords_table.c.id)
                .where(keywords_table.c.keyword.in_(query['keywords']))
            )
            conditions.append(files_table.c.md5_hash.in_(kw_subq))

        if query.get('country'):
            conditions.append(locations_table.c.country == query['country'])
        if query.get('date_from'):
            conditions.append(file_details_table.c.recorded_at >= query['date_from'])
        if query.get('date_to'):
            conditions.append(file_details_table.c.recorded_at <= query['date_to'])
        if query.get('camera_make'):
            conditions.append(photo_details_table.c.camera_make == query['camera_make'])
        if query.get('camera_model'):
            conditions.append(photo_details_table.c.camera_model == query['camera_model'])
        if query.get('video_codec'):
            conditions.append(video_details_table.c.video_codec == query['video_codec'])

        # Geographic bounding box (used by the map's "open in search" cluster link).
        # Matches the map's coordinate logic: named-location coords first, raw GPS
        # fallback. All four bounds must be present to apply.
        bbox = (query.get('bbox_west'), query.get('bbox_south'),
                query.get('bbox_east'), query.get('bbox_north'))
        if all(b is not None for b in bbox):
            w, s, e, n = bbox
            geo_lat = func.coalesce(locations_table.c.latitude, file_details_table.c.latitude)
            geo_lon = func.coalesce(locations_table.c.longitude, file_details_table.c.longitude)
            conditions.append(geo_lat.between(s, n))
            conditions.append(geo_lon.between(w, e))

        # List filter (OR semantics across the selected lists, like keywords) +
        # optional code, both applied via a subquery on ListItems so results
        # stay one row per file (no join fan-out).
        list_ids = query.get('list_ids') or []
        list_code = query.get('list_code')
        if list_ids or list_code:
            list_items_subq = select(list_items_table.c.md5_hash)
            if list_ids:
                list_items_subq = list_items_subq.where(list_items_table.c.list_id.in_(list_ids))
            if list_code:
                normalized_code = normalize_item_code(list_code)
                list_items_subq = list_items_subq.where(
                    list_items_table.c.item_code == normalized_code)
            conditions.append(files_table.c.md5_hash.in_(list_items_subq))

        base_from = (
            files_table
            .outerjoin(file_details_table,
                       files_table.c.md5_hash == file_details_table.c.md5_hash)
            .outerjoin(locations_table,
                       file_details_table.c.location_id == locations_table.c.id)
            .outerjoin(video_details_table,
                       files_table.c.md5_hash == video_details_table.c.md5_hash)
            .outerjoin(photo_details_table,
                       files_table.c.md5_hash == photo_details_table.c.md5_hash)
        )

        # When exactly one list is selected, also surface that list's item code
        # per result (e.g. for a badge on the result card).
        item_code_col = None
        if len(list_ids) == 1:
            item_code_subq = (
                select(list_items_table.c.item_code)
                .where(list_items_table.c.list_id == list_ids[0],
                       list_items_table.c.md5_hash == files_table.c.md5_hash)
                .scalar_subquery()
            )
            item_code_col = item_code_subq.label('item_code')

        count_stmt = select(func.count()).select_from(base_from)
        data_columns = [
            files_table.c.md5_hash, files_table.c.file_name,
            files_table.c.directory, files_table.c.media_type,
            file_details_table.c.recorded_at,
            locations_table.c.country, locations_table.c.city,
        ]
        if item_code_col is not None:
            data_columns.append(item_code_col)
        data_stmt = (
            select(*data_columns)
            .select_from(base_from)
            .order_by(
                file_details_table.c.recorded_at.desc().nullslast(),
                files_table.c.file_name,
            )
            .limit(query.get('page_size', 50))
            .offset((query.get('page', 1) - 1) * query.get('page_size', 50))
        )

        if conditions:
            count_stmt = count_stmt.where(*conditions)
            data_stmt = data_stmt.where(*conditions)

        with get_engine().connect() as conn:
            total = conn.execute(count_stmt).scalar()
            rows = conn.execute(data_stmt).fetchall()

        return total, [row._asdict() for row in rows]

    def get_all_keywords(self) -> list[str]:
        stmt = select(keywords_table.c.keyword).order_by(keywords_table.c.keyword)
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [row[0] for row in rows]

    def get_all_locations(self) -> list[dict]:
        stmt = select(locations_table).order_by(
            locations_table.c.country, locations_table.c.city, locations_table.c.name
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [row._asdict() for row in rows]

    def create_location(self, name: Optional[str], city: Optional[str], region: Optional[str],
                        country: Optional[str], latitude: Optional[float],
                        longitude: Optional[float]) -> int:
        with get_engine().begin() as conn:
            return conn.execute(
                locations_table.insert().returning(locations_table.c.id),
                {'name': name, 'city': city, 'region': region,
                 'country': country, 'latitude': latitude, 'longitude': longitude},
            ).scalar()

    def get_location_for_file(self, md5_hash: str) -> Optional[dict]:
        stmt = (
            select(locations_table)
            .join(file_details_table,
                  locations_table.c.id == file_details_table.c.location_id)
            .where(file_details_table.c.md5_hash == md5_hash)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).fetchone()
        return row._asdict() if row is not None else None

    def assign_location(self, md5_hash: str, location_id: Optional[int]) -> None:
        with get_engine().begin() as conn:
            conn.execute(
                upsert(file_details_table,
                       [{'md5_hash': md5_hash, 'location_id': location_id}],
                       ['md5_hash'])
            )

    def get_clip_preview(self, md5_hash: str) -> bytes | None:
        stmt = (
            select(clip_previews_table.c.data)
            .where(clip_previews_table.c.md5_hash == md5_hash)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).fetchone()
        return row[0] if row else None

    def get_files_without_clip_preview(self) -> pd.DataFrame:
        stmt = (
            select(
                files_table.c.md5_hash,
                files_table.c.file_name,
                (files_table.c.directory + '/' + files_table.c.file_name).label('file_path'),
            )
            .outerjoin(clip_previews_table,
                       files_table.c.md5_hash == clip_previews_table.c.md5_hash)
            .where(clip_previews_table.c.md5_hash.is_(None))
        )
        with get_engine().connect() as conn:
            return pd.read_sql_query(stmt, conn)

    def get_tracked_files_with_attachment_counts(self, directory: Optional[str] = None,
                                                  md5_hashes: Optional[list[str]] = None) -> list[dict]:
        """Every tracked Files row (optionally restricted to ``directory`` or
        anything below it — same escaped LIKE-prefix approach as
        count_tracked_files_under — and/or to a specific set of ``md5_hashes``,
        used by the path-conflicts listing), with what's "attached" to it:
        keyword count, whether a location is assigned, how many lists it's
        in, and whether a clip preview exists. One query (correlated-subquery
        counts + LEFT JOINs), no N+1."""
        keyword_count = (
            select(func.count())
            .select_from(file_keywords_table)
            .where(file_keywords_table.c.md5_hash == files_table.c.md5_hash)
            .scalar_subquery()
        )
        list_count = (
            select(func.count())
            .select_from(list_items_table)
            .where(list_items_table.c.md5_hash == files_table.c.md5_hash)
            .scalar_subquery()
        )
        stmt = (
            select(
                files_table.c.md5_hash,
                files_table.c.file_name,
                files_table.c.directory,
                files_table.c.media_type,
                keyword_count.label('keyword_count'),
                file_details_table.c.location_id.isnot(None).label('has_location'),
                list_count.label('list_count'),
                clip_previews_table.c.md5_hash.isnot(None).label('has_preview'),
            )
            .select_from(
                files_table
                .outerjoin(file_details_table,
                           files_table.c.md5_hash == file_details_table.c.md5_hash)
                .outerjoin(clip_previews_table,
                           files_table.c.md5_hash == clip_previews_table.c.md5_hash)
            )
        )
        if directory is not None:
            like_pattern = self._escape_like(directory) + '/%'
            stmt = stmt.where(
                (files_table.c.directory == directory)
                | files_table.c.directory.like(like_pattern, escape='\\')
            )
        if md5_hashes is not None:
            if not md5_hashes:
                return []
            stmt = stmt.where(files_table.c.md5_hash.in_(md5_hashes))
        stmt = stmt.order_by(files_table.c.directory, files_table.c.file_name)
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [row._asdict() for row in rows]

    # ------------------------------------------------------------------
    # Lists
    # ------------------------------------------------------------------

    def get_all_lists(self) -> list[dict]:
        item_count = (
            select(func.count())
            .select_from(list_items_table)
            .where(list_items_table.c.list_id == lists_table.c.id)
            .scalar_subquery()
        )
        stmt = (
            select(lists_table.c.id, lists_table.c.name, lists_table.c.created_at,
                   item_count.label('item_count'))
            .order_by(func.lower(lists_table.c.name))
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [row._asdict() for row in rows]

    def get_list(self, list_id: int) -> Optional[dict]:
        item_count = (
            select(func.count())
            .select_from(list_items_table)
            .where(list_items_table.c.list_id == lists_table.c.id)
            .scalar_subquery()
        )
        stmt = (
            select(lists_table.c.id, lists_table.c.name, lists_table.c.created_at,
                   item_count.label('item_count'))
            .where(lists_table.c.id == list_id)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).fetchone()
        return row._asdict() if row is not None else None

    def create_list(self, name: str) -> dict:
        try:
            with get_engine().begin() as conn:
                row = conn.execute(
                    lists_table.insert()
                    .values(name=name)
                    .returning(lists_table.c.id, lists_table.c.name, lists_table.c.created_at)
                ).fetchone()
        except IntegrityError:
            raise DuplicateListNameError(name)
        result = row._asdict()
        result['item_count'] = 0
        return result

    def rename_list(self, list_id: int, name: str) -> Optional[dict]:
        try:
            with get_engine().begin() as conn:
                row = conn.execute(
                    update(lists_table)
                    .where(lists_table.c.id == list_id)
                    .values(name=name)
                    .returning(lists_table.c.id, lists_table.c.name, lists_table.c.created_at)
                ).fetchone()
        except IntegrityError:
            raise DuplicateListNameError(name)
        if row is None:
            return None
        return self.get_list(list_id)

    def delete_list(self, list_id: int) -> bool:
        with get_engine().begin() as conn:
            conn.execute(delete(list_items_table).where(list_items_table.c.list_id == list_id))
            result = conn.execute(delete(lists_table).where(lists_table.c.id == list_id))
        return result.rowcount > 0

    def get_list_items(self, list_id: int, page: int, page_size: int) -> tuple[int, list[dict]]:
        base_from = list_items_table.join(
            files_table, list_items_table.c.md5_hash == files_table.c.md5_hash)
        count_stmt = (
            select(func.count())
            .select_from(list_items_table)
            .where(list_items_table.c.list_id == list_id)
        )
        data_stmt = (
            select(
                list_items_table.c.item_code, list_items_table.c.md5_hash,
                files_table.c.file_name, files_table.c.directory,
                files_table.c.media_type, list_items_table.c.added_at,
            )
            .select_from(base_from)
            .where(list_items_table.c.list_id == list_id)
            .order_by(list_items_table.c.added_at.desc(), list_items_table.c.item_code)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        with get_engine().connect() as conn:
            total = conn.execute(count_stmt).scalar()
            rows = conn.execute(data_stmt).fetchall()
        return total, [row._asdict() for row in rows]

    def add_files_to_list(self, list_id: int, md5_hashes: list[str]) -> dict:
        md5_hashes = list(dict.fromkeys(md5_hashes))
        if not md5_hashes:
            return {'added': [], 'existing': [], 'unknown': []}

        with get_engine().begin() as conn:
            existing_files = {
                r[0] for r in conn.execute(
                    select(files_table.c.md5_hash).where(files_table.c.md5_hash.in_(md5_hashes))
                ).fetchall()
            }
            unknown = [h for h in md5_hashes if h not in existing_files]
            valid_hashes = [h for h in md5_hashes if h in existing_files]

            existing_hashes = set()
            if valid_hashes:
                existing_hashes = {
                    r[0] for r in conn.execute(
                        select(list_items_table.c.md5_hash)
                        .where(list_items_table.c.list_id == list_id,
                               list_items_table.c.md5_hash.in_(valid_hashes))
                    ).fetchall()
                }
            to_add = [h for h in valid_hashes if h not in existing_hashes]

            used_codes = {
                r[0] for r in conn.execute(
                    select(list_items_table.c.item_code)
                    .where(list_items_table.c.list_id == list_id)
                ).fetchall()
            }

            for h in to_add:
                inserted = False
                for _ in range(6):
                    code = generate_item_code()
                    while code in used_codes:
                        code = generate_item_code()
                    try:
                        with conn.begin_nested():
                            conn.execute(
                                list_items_table.insert().values(
                                    list_id=list_id, md5_hash=h, item_code=code)
                            )
                        used_codes.add(code)
                        inserted = True
                        break
                    except IntegrityError:
                        continue
                if not inserted:
                    raise RuntimeError(
                        f'Could not generate a unique item code for list {list_id}')

            items_by_hash = {}
            if valid_hashes:
                items_stmt = (
                    select(
                        list_items_table.c.item_code, list_items_table.c.md5_hash,
                        files_table.c.file_name, files_table.c.directory,
                        files_table.c.media_type, list_items_table.c.added_at,
                    )
                    .select_from(list_items_table.join(
                        files_table, list_items_table.c.md5_hash == files_table.c.md5_hash))
                    .where(list_items_table.c.list_id == list_id,
                           list_items_table.c.md5_hash.in_(valid_hashes))
                )
                for row in conn.execute(items_stmt).fetchall():
                    items_by_hash[row.md5_hash] = row._asdict()

        added = [items_by_hash[h] for h in to_add if h in items_by_hash]
        existing = [items_by_hash[h] for h in valid_hashes
                    if h in existing_hashes and h in items_by_hash]
        return {'added': added, 'existing': existing, 'unknown': unknown}

    def remove_file_from_list(self, list_id: int, md5_hash: str) -> bool:
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(list_items_table)
                .where(list_items_table.c.list_id == list_id,
                       list_items_table.c.md5_hash == md5_hash)
            )
        return result.rowcount > 0

    def get_list_item_by_code(self, list_id: int, code: str) -> Optional[dict]:
        normalized = normalize_item_code(code)
        stmt = (
            select(
                list_items_table.c.item_code, list_items_table.c.md5_hash,
                files_table.c.file_name, files_table.c.directory,
                files_table.c.media_type, list_items_table.c.added_at,
            )
            .select_from(list_items_table.join(
                files_table, list_items_table.c.md5_hash == files_table.c.md5_hash))
            .where(list_items_table.c.list_id == list_id,
                   list_items_table.c.item_code == normalized)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).fetchone()
        return row._asdict() if row is not None else None

    def get_all_list_items_for_export(self, list_id: int) -> list[dict]:
        """All items of a list, sorted by item_code, with no pagination —
        used by the PDF card export which needs every item on one pass."""
        stmt = (
            select(
                list_items_table.c.item_code, list_items_table.c.md5_hash,
                files_table.c.file_name, files_table.c.directory,
                files_table.c.media_type, list_items_table.c.added_at,
            )
            .select_from(list_items_table.join(
                files_table, list_items_table.c.md5_hash == files_table.c.md5_hash))
            .where(list_items_table.c.list_id == list_id)
            .order_by(list_items_table.c.item_code)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [row._asdict() for row in rows]

    # ------------------------------------------------------------------
    # File operations journal + safe move/rename support (fileops/)
    # ------------------------------------------------------------------

    @staticmethod
    def _escape_like(value: str) -> str:
        """Escape a literal string for use in a LIKE pattern with ESCAPE '\\'."""
        return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

    def insert_file_operation(self, kind: str, source_path: str, target_path: str) -> int:
        """Insert a journal row in 'pending' status and commit immediately,
        *before* the physical rename it describes is attempted."""
        stmt = (
            file_operations_table.insert()
            .values(kind=kind, source_path=source_path, target_path=target_path, status='pending')
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.inserted_primary_key[0]

    def mark_file_operation(self, operation_id: int, status: str, error: Optional[str] = None,
                            finished: bool = True) -> None:
        values = {'status': status, 'error': error}
        if finished:
            values['finished_at'] = func.now()
        stmt = (
            update(file_operations_table)
            .where(file_operations_table.c.id == operation_id)
            .values(**values)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def get_pending_file_operations(self) -> list[dict]:
        stmt = (
            select(file_operations_table)
            .where(file_operations_table.c.status == 'pending')
            .order_by(file_operations_table.c.id)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [row._asdict() for row in rows]

    def run_guarded_rename(self, apply_updates: Callable[[object], None],
                           do_rename: Callable[[], None],
                           undo_rename: Callable[[], None]) -> None:
        """Runs ``apply_updates(conn)`` followed by ``do_rename()`` inside a
        single DB transaction.

        - If ``do_rename`` raises (e.g. the underlying ``os.rename`` failed),
          the transaction is rolled back and the exception re-raised — the
          caller marks the journal row 'failed'.
        - If the transaction commit itself fails *after* ``do_rename``
          already succeeded, ``undo_rename`` is called to reverse the
          physical rename, and the exception is re-raised — the caller
          marks the journal row 'rolled_back'.
        - If ``undo_rename`` itself then also raises, the filesystem and DB
          may now be inconsistent (the rename happened, the commit didn't,
          and reversing it failed too) — :class:`UndoRenameFailedError` is
          raised instead, so the caller knows NOT to mark the journal row
          'rolled_back' (or 'failed'); it stays 'pending' for
          recover_pending_operations() to reconcile on next startup.

        The connection is always closed, success or failure.
        """
        conn = get_engine().connect()
        try:
            trans = conn.begin()
            try:
                apply_updates(conn)
                do_rename()
            except Exception:
                trans.rollback()
                raise
            try:
                trans.commit()
            except Exception as commit_error:
                try:
                    undo_rename()
                except Exception as undo_error:
                    raise UndoRenameFailedError(commit_error, undo_error) from undo_error
                raise
        finally:
            conn.close()

    def update_file_path_on_conn(self, conn, old_directory: str, old_file_name: str,
                                 new_directory: str, new_file_name: str) -> bool:
        """Point the Files row matching (old_directory, old_file_name) at the
        new location. No-op (returns False) if no tracked row matches —
        untracked files are only moved on disk."""
        stmt = (
            update(files_table)
            .where(files_table.c.directory == old_directory,
                   files_table.c.file_name == old_file_name)
            .values(directory=new_directory, file_name=new_file_name)
        )
        result = conn.execute(stmt)
        return result.rowcount > 0

    def update_directory_prefix_on_conn(self, conn, old_directory: str, new_directory: str) -> int:
        """Prefix-update Files.directory for every row at ``old_directory`` or
        below it (``old_directory`` itself, or anything under
        ``old_directory + '/'``). `/a/bc` is never matched by a rename of
        `/a/b` — the LIKE pattern is escaped and anchored with a trailing
        '/'."""
        like_pattern = self._escape_like(old_directory) + '/%'
        start_pos = len(old_directory) + 1  # 1-indexed SQL substr position
        stmt = (
            update(files_table)
            .where(
                (files_table.c.directory == old_directory)
                | files_table.c.directory.like(like_pattern, escape='\\')
            )
            .values(directory=new_directory + func.substr(files_table.c.directory, start_pos))
        )
        result = conn.execute(stmt)
        return result.rowcount

    def update_file_path(self, old_directory: str, old_file_name: str,
                         new_directory: str, new_file_name: str) -> bool:
        """Standalone (own-transaction) variant of update_file_path_on_conn,
        used by recovery which runs outside the move/rename flow."""
        with get_engine().begin() as conn:
            return self.update_file_path_on_conn(conn, old_directory, old_file_name,
                                                 new_directory, new_file_name)

    def update_directory_prefix(self, old_directory: str, new_directory: str) -> int:
        """Standalone (own-transaction) variant of update_directory_prefix_on_conn,
        used by recovery which runs outside the move/rename flow."""
        with get_engine().begin() as conn:
            return self.update_directory_prefix_on_conn(conn, old_directory, new_directory)

    def count_tracked_files_under(self, directory: str) -> int:
        """Count Files rows at exactly ``directory`` or anywhere below it."""
        like_pattern = self._escape_like(directory) + '/%'
        stmt = (
            select(func.count())
            .select_from(files_table)
            .where(
                (files_table.c.directory == directory)
                | files_table.c.directory.like(like_pattern, escape='\\')
            )
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).scalar_one()

    def get_lists_for_file(self, md5_hash: str) -> list[dict]:
        stmt = (
            select(list_items_table.c.list_id, lists_table.c.name,
                   list_items_table.c.item_code)
            .select_from(list_items_table.join(
                lists_table, list_items_table.c.list_id == lists_table.c.id))
            .where(list_items_table.c.md5_hash == md5_hash)
            .order_by(func.lower(lists_table.c.name))
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [row._asdict() for row in rows]

    def delete_files_on_conn(self, conn, md5_hashes: list[str]) -> int:
        """Remove the given hashes from the archive entirely, on a
        caller-supplied connection (so it can share a transaction with a
        physical rename — see fileops/service.py's trash flow): the Files
        row plus everything keyed on it (details, keywords, list
        memberships, clip preview, path conflicts). Locations and Keywords
        themselves are shared and stay."""
        if not md5_hashes:
            return 0
        dependent_tables = (
            file_keywords_table, list_items_table, path_conflicts_table,
            clip_previews_table, video_details_table, photo_details_table,
            file_details_table,
        )
        for table in dependent_tables:
            conn.execute(delete(table).where(table.c.md5_hash.in_(md5_hashes)))
        result = conn.execute(delete(files_table).where(files_table.c.md5_hash.in_(md5_hashes)))
        return result.rowcount

    def delete_files(self, md5_hashes: list[str]) -> int:
        """Standalone (own-transaction) variant of delete_files_on_conn."""
        if not md5_hashes:
            return 0
        with get_engine().begin() as conn:
            return self.delete_files_on_conn(conn, md5_hashes)

    def get_hash_by_path_on_conn(self, conn, directory: str, file_name: str) -> Optional[str]:
        """md5_hash for a tracked (directory, file_name), or None if
        untracked. Used inside a guarded-rename transaction (trash flow) to
        find what to delete before the physical rename commits."""
        stmt = (
            select(files_table.c.md5_hash)
            .where(files_table.c.directory == directory, files_table.c.file_name == file_name)
        )
        return conn.execute(stmt).scalar()

    def get_hashes_under_directory_on_conn(self, conn, directory: str) -> list[str]:
        """md5_hash values tracked at exactly ``directory`` or anywhere below
        it (same escaped LIKE-prefix approach as count_tracked_files_under /
        update_directory_prefix_on_conn — `/a/bc` is never matched by
        `/a/b`). Used inside a guarded-rename transaction to find what to
        delete before a directory's physical rename into the trash commits."""
        like_pattern = self._escape_like(directory) + '/%'
        stmt = (
            select(files_table.c.md5_hash)
            .where(
                (files_table.c.directory == directory)
                | files_table.c.directory.like(like_pattern, escape='\\')
            )
        )
        return [row[0] for row in conn.execute(stmt).fetchall()]

    def delete_path_conflicts_by_candidate_paths_on_conn(self, conn, candidate_paths: list[str]) -> int:
        """Delete PathConflicts rows (for any hash) whose candidate_path is
        exactly one of ``candidate_paths`` — used when those paths have just
        moved into the trash, so a stale conflict never points there."""
        if not candidate_paths:
            return 0
        result = conn.execute(
            delete(path_conflicts_table)
            .where(path_conflicts_table.c.candidate_path.in_(candidate_paths))
        )
        return result.rowcount

    def finish_pending_trash_delete(self, kind: str, source_path: str) -> None:
        """Idempotently remove tracking for a recovered ``file_trash``/
        ``dir_trash`` journal row where the target was found on disk and the
        source wasn't: the physical rename already happened, but the DB side
        of that same transaction never committed before the crash. Safe to
        call even if the DB is already up to date (nothing matches)."""
        source = Path(source_path)
        with get_engine().begin() as conn:
            if kind == 'dir_trash':
                hashes = self.get_hashes_under_directory_on_conn(conn, source_path)
                self.delete_files_on_conn(conn, hashes)
                self.delete_path_conflicts_under_on_conn(conn, source_path)
            else:
                md5 = self.get_hash_by_path_on_conn(conn, str(source.parent), source.name)
                if md5:
                    self.delete_files_on_conn(conn, [md5])
                self.delete_path_conflicts_by_candidate_paths_on_conn(conn, [source_path])

    def delete_path_conflicts_under_on_conn(self, conn, directory: str) -> int:
        """Delete PathConflicts rows (for any hash) whose candidate_path is
        ``directory`` itself or anywhere below it (escaped LIKE prefix, same
        anchoring as get_hashes_under_directory_on_conn)."""
        like_pattern = self._escape_like(directory) + '/%'
        result = conn.execute(
            delete(path_conflicts_table)
            .where(
                (path_conflicts_table.c.candidate_path == directory)
                | path_conflicts_table.c.candidate_path.like(like_pattern, escape='\\')
            )
        )
        return result.rowcount

    # ------------------------------------------------------------------
    # Rediscover (fileops/rediscover.py) + path conflicts
    # ------------------------------------------------------------------

    def get_tracked_paths_for_hashes(self, md5_hashes: list[str]) -> dict[str, dict]:
        """md5_hash -> {'directory', 'file_name'} for the given hashes that
        are currently tracked in Files. Hashes not tracked are simply absent
        from the result."""
        if not md5_hashes:
            return {}
        stmt = (
            select(files_table.c.md5_hash, files_table.c.directory, files_table.c.file_name)
            .where(files_table.c.md5_hash.in_(md5_hashes))
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {row.md5_hash: {'directory': row.directory, 'file_name': row.file_name} for row in rows}

    def relink_files(self, relinks: list[dict]) -> None:
        """Point each row's (directory, file_name, file_extension) at its new
        location. ``relinks`` is a list of
        {'md5_hash', 'old_directory', 'old_file_name', 'directory', 'file_name',
        'file_extension'}. A row is only updated if it still points at the old
        path, so a concurrent change since classification is never overwritten.
        Applied as one transaction for all relinks of a single rediscover run.
        Metadata tables are never touched."""
        if not relinks:
            return
        with get_engine().begin() as conn:
            for r in relinks:
                conn.execute(
                    update(files_table)
                    .where(files_table.c.md5_hash == r['md5_hash'],
                           files_table.c.directory == r['old_directory'],
                           files_table.c.file_name == r['old_file_name'])
                    .values(directory=r['directory'], file_name=r['file_name'],
                            file_extension=r['file_extension'])
                )

    def insert_path_conflicts(self, conflicts: list[dict], source: str) -> None:
        """``conflicts`` is a list of {'md5_hash', 'candidate_path'}.
        ON CONFLICT DO NOTHING on the (md5_hash, candidate_path) primary key —
        re-running a rediscover never duplicates an already-known conflict."""
        if not conflicts:
            return
        records = [
            {'md5_hash': c['md5_hash'], 'candidate_path': c['candidate_path'], 'source': source}
            for c in conflicts
        ]
        with get_engine().begin() as conn:
            conn.execute(
                upsert_ignore(path_conflicts_table, records, ['md5_hash', 'candidate_path'])
            )

    def get_path_conflicts_for_pruning(self, md5_hashes: list[str], directory: str) -> list[dict]:
        """PathConflicts rows worth checking for staleness after a rediscover
        of ``directory``: rows for any of ``md5_hashes`` (the hashes touched
        by this run), plus rows whose candidate_path sits under ``directory``
        (so a conflict left behind by an earlier run on this folder, for a
        hash not touched this time, still gets reconsidered). This is a
        bounded, predictable scope — it does not scan the whole table."""
        like_pattern = self._escape_like(directory) + '/%'
        under_directory = (
            (path_conflicts_table.c.candidate_path == directory)
            | path_conflicts_table.c.candidate_path.like(like_pattern, escape='\\')
        )
        condition = under_directory
        if md5_hashes:
            condition = path_conflicts_table.c.md5_hash.in_(md5_hashes) | under_directory
        stmt = select(path_conflicts_table).where(condition)
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [row._asdict() for row in rows]

    def delete_path_conflicts(self, pairs: list[tuple[str, str]]) -> int:
        """Delete specific (md5_hash, candidate_path) PathConflicts rows."""
        if not pairs:
            return 0
        stmt = delete(path_conflicts_table).where(
            tuple_(path_conflicts_table.c.md5_hash, path_conflicts_table.c.candidate_path).in_(pairs)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
        return result.rowcount

    def get_path_conflicts(self, md5_hash: Optional[str] = None) -> list[dict]:
        stmt = select(path_conflicts_table)
        if md5_hash is not None:
            stmt = stmt.where(path_conflicts_table.c.md5_hash == md5_hash)
        stmt = stmt.order_by(path_conflicts_table.c.md5_hash, path_conflicts_table.c.candidate_path)
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [row._asdict() for row in rows]

    def get_distinct_conflict_hashes(self) -> list[str]:
        """md5_hash values currently carrying one or more open PathConflicts
        rows, used to build the grouped conflict listing (GET /tracking/conflicts)."""
        stmt = select(path_conflicts_table.c.md5_hash).distinct().order_by(path_conflicts_table.c.md5_hash)
        with get_engine().connect() as conn:
            return [row[0] for row in conn.execute(stmt).fetchall()]

    def count_distinct_conflicts(self) -> int:
        """Number of distinct md5_hash values with open conflicts — backs the
        sidebar badge (GET /tracking/conflicts/count)."""
        stmt = select(func.count(func.distinct(path_conflicts_table.c.md5_hash)))
        with get_engine().connect() as conn:
            return conn.execute(stmt).scalar_one()

    def resolve_path_conflict(self, md5_hash: str, old_directory: str, old_file_name: str,
                              new_directory: Optional[str] = None, new_file_name: Optional[str] = None,
                              new_file_extension: Optional[str] = None) -> None:
        """Resolve a path conflict for ``md5_hash`` in one transaction: if a
        new path is given (the chosen path differs from the currently tracked
        one), repoint the Files row — guarded on the old path, like
        relink_files — then unconditionally delete every PathConflicts row
        for this hash. Disk is never touched."""
        with get_engine().begin() as conn:
            if new_directory is not None:
                result = conn.execute(
                    update(files_table)
                    .where(files_table.c.md5_hash == md5_hash,
                           files_table.c.directory == old_directory,
                           files_table.c.file_name == old_file_name)
                    .values(directory=new_directory, file_name=new_file_name,
                            file_extension=new_file_extension)
                )
                if result.rowcount == 0:
                    # Tracked path changed since the caller read it — don't
                    # drop the conflict rows on a no-op; let the caller retry.
                    raise StaleConflictError(md5_hash)
            conn.execute(delete(path_conflicts_table).where(path_conflicts_table.c.md5_hash == md5_hash))
