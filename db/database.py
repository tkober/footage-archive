import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from sqlalchemy import and_, case, delete, func, select, tuple_, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import aggregate_order_by, insert as pg_insert
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
    preview_status_table,
    scan_jobs_table,
    scan_units_table,
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


class ScanJobNotFoundError(Exception):
    """Raised by the Scan queue (#137) methods for an unknown ScanJobs id."""


class ScanUnitNotFoundError(Exception):
    """Raised by the Scan queue (#137) methods for a unit id that doesn't
    exist, or doesn't belong to the given job."""


class InvalidScanTransitionError(Exception):
    """Raised by the Scan queue (#137) methods for a state transition that
    isn't valid from the job's/unit's current status. Carries a
    human-readable `detail` the API surfaces as a 409 response."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class DirectoryAlreadyQueuedError(Exception):
    """Raised by retry_scan_unit() when requeuing a unit would violate the
    partial unique index on ScanUnits.directory — the directory is already
    QUEUED/RUNNING in another job. The API maps this to 409."""

    def __init__(self, unit_id: int):
        self.unit_id = unit_id
        super().__init__(f'Directory already queued or running in another job (unit {unit_id})')


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
        # size_bytes/mtime_ns (#136) are written only once a file's probe
        # step finishes successfully (see Database.set_file_signatures) —
        # never by this pre-probe upsert, not even with NULL, so a crash
        # between this insert and the probe leaves the old/NULL signature in
        # place and the next incremental scan re-hashes the file instead of
        # skipping it forever.
        df = df.drop(columns=['size_bytes', 'mtime_ns'], errors='ignore')
        records = _df_to_records(df, files_table)
        if records:
            with get_engine().begin() as conn:
                conn.execute(upsert(files_table, records, ['md5_hash']))

    def get_file_signatures_in_directory(self, directory: str) -> dict[str, set[tuple[int, int]]]:
        """(size_bytes, mtime_ns) signatures of every tracked file at exactly
        `directory` (not recursive), keyed by file_name — the incremental
        scan's skip-rule lookup (#136), one query per directory instead of
        per file or per batch. `Files` has no uniqueness on (directory,
        file_name): a file whose content changed is a new hash, so several
        rows can share a path — all of them are returned, so a candidate
        matches if ANY one of them carries exactly its signature. A row with
        a NULL size_bytes or mtime_ns (never probed since the migration, or
        a signature write that never completed) is excluded — a NULL
        signature can never "match" a real candidate anyway."""
        stmt = (
            select(files_table.c.file_name, files_table.c.size_bytes, files_table.c.mtime_ns)
            .where(files_table.c.directory == directory,
                   files_table.c.size_bytes.isnot(None),
                   files_table.c.mtime_ns.isnot(None))
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result: dict[str, set[tuple[int, int]]] = {}
        for row in rows:
            result.setdefault(row.file_name, set()).add((row.size_bytes, row.mtime_ns))
        return result

    def set_file_signatures(self, rows: list[dict]) -> None:
        """Write Files.size_bytes/mtime_ns (#136) for files whose probe step
        has just finished successfully — see the CRASH-SAFETY note on
        api/tracking.py's `_scan_and_reconcile`/`_save_signatures` for why
        this must never run before that. Guarded on (md5_hash, directory,
        file_name) — the row's path *right now*, as just inserted/relinked
        by this same scan — so a concurrent relink onto this hash between
        the insert and this call is never overwritten with a stale
        signature. ``rows`` is a list of {'md5_hash', 'directory',
        'file_name', 'size_bytes', 'mtime_ns'}."""
        if not rows:
            return
        with get_engine().begin() as conn:
            for r in rows:
                conn.execute(
                    update(files_table)
                    .where(files_table.c.md5_hash == r['md5_hash'],
                           files_table.c.directory == r['directory'],
                           files_table.c.file_name == r['file_name'])
                    .values(size_bytes=r['size_bytes'], mtime_ns=r['mtime_ns'])
                )

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

    def set_preview_status(self, md5_hash: str, status: str, reason: Optional[str] = None) -> None:
        """Upsert the outcome of a preview-generation attempt (#77):
        'ok' | 'failed' | 'unsupported', with attempted_at bumped to now.
        The single caller is api/tracking.py's generate_preview (incl. the
        create_clip_preview helper it shares with the DaVinci import path),
        so there's exactly one place that decides a preview's fate and one
        place that records it."""
        with get_engine().begin() as conn:
            conn.execute(upsert(
                preview_status_table,
                [{'md5_hash': md5_hash, 'status': status, 'reason': reason, 'attempted_at': func.now()}],
                ['md5_hash'],
            ))

    def get_preview_status_row(self, md5_hash: str) -> Optional[dict]:
        stmt = select(preview_status_table).where(preview_status_table.c.md5_hash == md5_hash)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).fetchone()
        return row._asdict() if row is not None else None

    def has_clip_preview(self, md5_hash: str) -> bool:
        stmt = select(clip_previews_table.c.md5_hash).where(clip_previews_table.c.md5_hash == md5_hash)
        with get_engine().connect() as conn:
            return conn.execute(stmt).fetchone() is not None

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_tracked_files_in_directory(self, directory: str) -> dict:
        stmt = (
            select(
                files_table.c.file_name, files_table.c.md5_hash, files_table.c.media_type,
                video_details_table.c.duration_tc,
                clip_previews_table.c.md5_hash.isnot(None).label('has_preview'),
                preview_status_table.c.status.label('preview_status'),
            )
            .select_from(
                files_table
                .outerjoin(
                    video_details_table,
                    files_table.c.md5_hash == video_details_table.c.md5_hash,
                )
                .outerjoin(
                    clip_previews_table,
                    files_table.c.md5_hash == clip_previews_table.c.md5_hash,
                )
                .outerjoin(
                    preview_status_table,
                    files_table.c.md5_hash == preview_status_table.c.md5_hash,
                )
            )
            .where(files_table.c.directory == directory)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {
            row.file_name: {
                'md5_hash': row.md5_hash, 'media_type': row.media_type, 'duration_tc': row.duration_tc,
                'has_preview': row.has_preview, 'preview_status': row.preview_status,
            }
            for row in rows
        }

    def count_tracked_files_by_directory(self, directories: list[str]) -> dict[str, int]:
        """Tracked-file count per directory (#134), for the browser's
        untracked-badge: one `GROUP BY` query over `Files.directory`
        (`idx__Files__directory` covers the `IN (...)`/grouping), called once
        per `/files/directory` request with every child folder instead of
        once per child. A directory with no tracked files is simply absent
        from the result — callers treat a missing key as 0. Comparison is
        exact-string, matching the same `str(child_path)` used elsewhere for
        `PathChild.path`/`get_tracked_files_in_directory`."""
        if not directories:
            return {}
        stmt = (
            select(files_table.c.directory, func.count().label('count'))
            .where(files_table.c.directory.in_(directories))
            .group_by(files_table.c.directory)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {row.directory: row.count for row in rows}

    def touch_last_indexed_at(self, md5_hash: str) -> None:
        """Bump Files.last_indexed_at to now for an already-tracked hash,
        without touching any other column. Used by the rescan task (#64),
        which builds a ScanResult from the existing Files row instead of
        re-hashing, so the normal insert_scan_results() upsert (which would
        also bump this) never runs."""
        with get_engine().begin() as conn:
            conn.execute(
                update(files_table)
                .where(files_table.c.md5_hash == md5_hash)
                .values(last_indexed_at=func.now())
            )

    def set_media_type(self, md5_hash: str, media_type: str | None) -> None:
        """Update Files.media_type for an already-tracked hash (#79) — used
        by `_probe_and_save` (api/tracking.py) to correct a file's
        extension-based classification once its metadata (EXIF make /
        projection tag) has actually been probed, without touching any
        other column (notably not `last_indexed_at`, which the caller
        already bumps itself via `insert_scan_results`/`touch_last_indexed_at`)."""
        with get_engine().begin() as conn:
            conn.execute(
                update(files_table)
                .where(files_table.c.md5_hash == md5_hash)
                .values(media_type=media_type)
            )

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
            # 0/0 is Insta360's "no GPS fix" sentinel (#91), not a real position —
            # old rows pre-dating the migration that clears them can still have it.
            if row[0] == 0 and row[1] == 0:
                return None
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
                file_details_table.c.recorded_at,
                locations_table.c.city,
                locations_table.c.country,
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
                # 0/0 is Insta360's "no GPS fix" sentinel (#91), not a real
                # position — exclude it even for rows pre-dating the migration
                # that clears it.
                ~and_(coalesce_lat == 0, coalesce_lon == 0),
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

        # Preview members, newest first (NULL recorded_at last), capped at 7
        # *inside SQL* — aggregating every member and trimming in Python would
        # mean pulling and discarding the full member list for every large
        # cluster. `recorded_at` is EXIF text (`YYYY:MM:DD HH:MM:SS`), so it
        # sorts correctly as text; md5_hash breaks ties deterministically.
        member_order = aggregate_order_by(
            func.json_build_object(
                'md5_hash', subq.c.md5_hash,
                'file_name', subq.c.file_name,
                'directory', subq.c.directory,
                'media_type', subq.c.media_type,
            ),
            subq.c.recorded_at.desc().nullslast(),
            subq.c.md5_hash.asc(),
        )
        members_expr = func.to_json(
            func.array_agg(member_order, type_=postgresql.ARRAY(postgresql.JSONB))[1:7]
        )

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
                # Date range over every member (min/max ignore NULLs; None if
                # no member in the cluster has a recorded_at at all).
                func.min(subq.c.recorded_at).label('date_from'),
                func.max(subq.c.recorded_at).label('date_to'),
                # Most common place name among members: city, falling back to
                # country when no member has a city, else None. mode() WITHIN
                # GROUP ignores NULL inputs as long as at least one row isn't NULL.
                func.coalesce(
                    func.mode().within_group(subq.c.city),
                    func.mode().within_group(subq.c.country),
                ).label('place'),
                # Per-member preview, at most 7, newest first — every cluster
                # gets one now (not just small all-stills leaves), so the map
                # can show a preview strip regardless of cluster size/content.
                members_expr.label('members'),
            )
            .select_from(subq)
            .group_by(lat_cell, lon_cell)
        )

        with get_engine().connect() as conn:
            rows = conn.execute(cluster_stmt).fetchall()
        return [row._asdict() for row in rows]

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
            # 0/0 is Insta360's "no GPS fix" sentinel (#91), not a real position.
            conditions.append(~and_(geo_lat == 0, geo_lon == 0))

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
            .outerjoin(clip_previews_table,
                       files_table.c.md5_hash == clip_previews_table.c.md5_hash)
            .outerjoin(preview_status_table,
                       files_table.c.md5_hash == preview_status_table.c.md5_hash)
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
            clip_previews_table.c.md5_hash.isnot(None).label('has_preview'),
            preview_status_table.c.status.label('preview_status_raw'),
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

    def get_files_without_clip_preview(self, media_types: set[str],
                                        include_failed: bool = False) -> pd.DataFrame:
        """Tracked files with no row in ClipPreviews, restricted to
        ``media_types`` (the caller passes video/photo media types, incl.
        360 — see api/tracking.py's VIDEO_TYPES/PHOTO_TYPES — so a
        non-media file, media_type NULL, never gets a preview and never
        bloats this list, #65). Also returns media_type so the caller knows
        which kind of preview to generate without a second query.

        ``preview_status``/``reason``/``attempted_at`` (#77) come from the
        PreviewStatus table and are NULL for a file that was never
        attempted. By default (``include_failed=False``) only those
        never-attempted rows are returned — a 'failed'/'unsupported'
        PreviewStatus row means the repair already tried and the file is
        excluded, so a retry has to opt in via ``include_failed=True``."""
        stmt = (
            select(
                files_table.c.md5_hash,
                files_table.c.file_name,
                files_table.c.media_type,
                (files_table.c.directory + '/' + files_table.c.file_name).label('file_path'),
                preview_status_table.c.status.label('preview_status'),
                preview_status_table.c.reason,
                preview_status_table.c.attempted_at,
            )
            .select_from(
                files_table
                .outerjoin(clip_previews_table,
                           files_table.c.md5_hash == clip_previews_table.c.md5_hash)
                .outerjoin(preview_status_table,
                           files_table.c.md5_hash == preview_status_table.c.md5_hash)
            )
            .where(clip_previews_table.c.md5_hash.is_(None))
            .where(files_table.c.media_type.in_(media_types))
        )
        if not include_failed:
            stmt = stmt.where(preview_status_table.c.md5_hash.is_(None))
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
        base_from = (
            list_items_table
            .join(files_table, list_items_table.c.md5_hash == files_table.c.md5_hash)
            .outerjoin(clip_previews_table,
                       files_table.c.md5_hash == clip_previews_table.c.md5_hash)
            .outerjoin(preview_status_table,
                       files_table.c.md5_hash == preview_status_table.c.md5_hash)
        )
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
                clip_previews_table.c.md5_hash.isnot(None).label('has_preview'),
                preview_status_table.c.status.label('preview_status_raw'),
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
            clip_previews_table, preview_status_table, video_details_table,
            photo_details_table, file_details_table,
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

    # ------------------------------------------------------------------
    # Scan queue (#137) — ScanJobs/ScanUnits, consumed by tasks/scanqueue.py
    # ------------------------------------------------------------------

    _ACTIVE_UNIT_STATUSES = ('QUEUED', 'RUNNING')  # must match the partial unique index's predicate
    _TERMINAL_UNIT_STATUSES = ('DONE', 'FAILED', 'CANCELLED')
    _TERMINAL_JOB_STATUSES = ('DONE', 'FAILED', 'CANCELLED')

    def create_scan_job(self, root_path: str, units: list[dict], options: dict,
                        start: bool) -> tuple[dict, list[dict]]:
        """Creates a ScanJob with its units in their given order (`position`
        0, 1, 2, …) — used by tests directly today and by #138's tree
        planner later. `units` is a list of
        {'directory', 'media_file_count'?, 'tracked_file_count'?}.

        `start=True` inserts the job QUEUED and every unit QUEUED straight
        away; `start=False` (the default a planner would use) inserts the
        job and every unit PLANNED, deselect-able via deselect_scan_unit()
        before a later start_scan_job() call.

        A QUEUED insert for a directory already QUEUED/RUNNING in another
        job is rejected by the partial unique index — caught per unit (via
        `ON CONFLICT ... DO NOTHING`, matching that index's predicate
        exactly) and reported back as `skipped`, instead of failing the
        whole job. A PLANNED insert never conflicts (the index only
        constrains QUEUED/RUNNING rows), so this can't happen when
        `start=False`."""
        job_id = str(uuid.uuid4())
        status = 'QUEUED' if start else 'PLANNED'
        skipped: list[dict] = []
        with get_engine().begin() as conn:
            conn.execute(scan_jobs_table.insert().values(
                id=job_id, root_path=root_path, status=status, options=options,
            ))
            for position, unit in enumerate(units):
                stmt = pg_insert(scan_units_table).values(
                    job_id=job_id, directory=unit['directory'], position=position, status=status,
                    media_file_count=unit.get('media_file_count'),
                    tracked_file_count=unit.get('tracked_file_count'),
                )
                if start:
                    stmt = stmt.on_conflict_do_nothing(
                        index_elements=['directory'],
                        index_where=scan_units_table.c.status.in_(self._ACTIVE_UNIT_STATUSES),
                    )
                stmt = stmt.returning(scan_units_table.c.id)
                if conn.execute(stmt).fetchone() is None:
                    skipped.append({'directory': unit['directory'], 'reason': 'already queued'})
        return self.get_scan_job(job_id), skipped

    def get_directories_with_active_scan_units(self, directories: list[str]) -> set[str]:
        """Which of `directories` already have a QUEUED/RUNNING ScanUnit in
        ANY job, right now (#138's tree planner, `scanner/walker.py` +
        `api/tracking.py`): a PLANNED insert never conflicts with the
        partial unique index (only a QUEUED one does, see create_scan_job's
        docstring), so a planner creating a PLANNED job must exclude these
        directories itself — it can't rely on create_scan_job's own
        ON-CONFLICT dedupe to catch them. One query for the whole candidate
        set, not one per directory."""
        if not directories:
            return set()
        stmt = (
            select(scan_units_table.c.directory)
            .where(scan_units_table.c.directory.in_(directories),
                   scan_units_table.c.status.in_(self._ACTIVE_UNIT_STATUSES))
            .distinct()
        )
        with get_engine().connect() as conn:
            return {row[0] for row in conn.execute(stmt).fetchall()}

    def recompute_scan_job_status(self, job_id: str, summarize: Callable[[list[dict]], str]) -> None:
        """Public, own-transaction wrapper around
        `_recompute_scan_job_status_on_conn` — used by `POST
        /tracking/scan-directory`'s 0-unit case (#138): a plan started
        immediately with no units to run would otherwise sit QUEUED
        forever, since nothing would ever call `finish_scan_unit` to
        trigger this recompute."""
        with get_engine().begin() as conn:
            self._recompute_scan_job_status_on_conn(conn, job_id, summarize)

    def delete_stale_planned_jobs(self, max_age: timedelta = timedelta(hours=24)) -> int:
        """Startup cleanup (#138, called from `tasks/scanqueue.py`'s
        `recover_on_startup`, before consumers start, alongside #137's
        `recover_scan_queue`): a PLANNED job the user never started (or
        forgot to discard) would otherwise pile up forever — delete any
        PLANNED job whose `created_at` is older than `max_age`. Units
        cascade (`ON DELETE CASCADE`). Returns how many jobs were deleted."""
        cutoff = datetime.now(timezone.utc) - max_age
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(scan_jobs_table)
                .where(scan_jobs_table.c.status == 'PLANNED', scan_jobs_table.c.created_at < cutoff)
            )
        return result.rowcount

    def get_scan_job(self, job_id: str) -> Optional[dict]:
        """One job with every one of its units, ordered by `position`. No
        aggregated progress here (see get_scan_jobs for that) — the detail
        view always has the full unit list to compute from if it needs to."""
        with get_engine().connect() as conn:
            job_row = conn.execute(select(scan_jobs_table).where(scan_jobs_table.c.id == job_id)).fetchone()
            if job_row is None:
                return None
            unit_rows = conn.execute(
                select(scan_units_table)
                .where(scan_units_table.c.job_id == job_id)
                .order_by(scan_units_table.c.position)
            ).fetchall()
        job = job_row._asdict()
        job['units'] = [r._asdict() for r in unit_rows]
        return job

    def get_scan_jobs(self, active: Optional[bool] = None) -> list[dict]:
        """Every job, newest first, with aggregated progress: `units_done`/
        `units_total` and `files_done`/`files_total`. "done" = a terminal
        unit (DONE/FAILED/CANCELLED); the files counts sum `media_file_count`
        over terminal units (`files_done`) vs. every non-DESELECTED unit
        (`files_total`) — a DESELECTED unit was taken out of the plan, so it
        counts toward neither; `units_total` uses the same non-DESELECTED
        definition for consistency. `active=True` restricts to jobs whose
        status isn't terminal; `active=False`/omitted returns every job."""
        done_expr = scan_units_table.c.status.in_(self._TERMINAL_UNIT_STATUSES)
        not_deselected_expr = scan_units_table.c.status != 'DESELECTED'
        agg = (
            select(
                scan_units_table.c.job_id,
                func.count().filter(done_expr).label('units_done'),
                func.count().filter(not_deselected_expr).label('units_total'),
                func.coalesce(
                    func.sum(case((done_expr, scan_units_table.c.media_file_count), else_=0)), 0
                ).label('files_done'),
                func.coalesce(
                    func.sum(case((not_deselected_expr, scan_units_table.c.media_file_count), else_=0)), 0
                ).label('files_total'),
            )
            .group_by(scan_units_table.c.job_id)
            .subquery()
        )
        stmt = (
            select(
                scan_jobs_table,
                func.coalesce(agg.c.units_done, 0).label('units_done'),
                func.coalesce(agg.c.units_total, 0).label('units_total'),
                func.coalesce(agg.c.files_done, 0).label('files_done'),
                func.coalesce(agg.c.files_total, 0).label('files_total'),
            )
            .select_from(scan_jobs_table.outerjoin(agg, scan_jobs_table.c.id == agg.c.job_id))
            .order_by(scan_jobs_table.c.created_at.desc())
        )
        if active is True:
            stmt = stmt.where(scan_jobs_table.c.status.notin_(self._TERMINAL_JOB_STATUSES))
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [row._asdict() for row in rows]

    def get_scan_job_status(self, job_id: str) -> Optional[str]:
        with get_engine().connect() as conn:
            row = conn.execute(select(scan_jobs_table.c.status).where(scan_jobs_table.c.id == job_id)).fetchone()
        return row.status if row is not None else None

    def claim_next_scan_unit(self) -> Optional[dict]:
        """The consumer's claim step (#137), one transaction: lock the next
        QUEUED unit of a QUEUED/RUNNING job (`FOR UPDATE OF ... SKIP LOCKED`
        — two consumers never claim the same row), set it RUNNING, and bump
        its job to RUNNING too (recording `started_at` the first time) —
        all before the caller ever starts the actual scan. Returns None if
        nothing is claimable right now."""
        now = datetime.now(timezone.utc)
        with get_engine().begin() as conn:
            stmt = (
                select(scan_units_table.c.id, scan_units_table.c.job_id, scan_units_table.c.directory,
                       scan_jobs_table.c.options)
                .select_from(scan_units_table.join(scan_jobs_table, scan_jobs_table.c.id == scan_units_table.c.job_id))
                .where(scan_units_table.c.status == 'QUEUED',
                       scan_jobs_table.c.status.in_(('QUEUED', 'RUNNING')))
                .order_by(scan_jobs_table.c.created_at, scan_units_table.c.position, scan_units_table.c.id)
                .limit(1)
                .with_for_update(of=scan_units_table, skip_locked=True)
            )
            row = conn.execute(stmt).fetchone()
            if row is None:
                return None
            conn.execute(
                update(scan_units_table)
                .where(scan_units_table.c.id == row.id)
                .values(status='RUNNING', started_at=now, error=None, progress=None)
            )
            job_row = conn.execute(
                select(scan_jobs_table.c.status, scan_jobs_table.c.started_at)
                .where(scan_jobs_table.c.id == row.job_id)
                .with_for_update()
            ).fetchone()
            if job_row.status == 'QUEUED':
                values = {'status': 'RUNNING'}
                if job_row.started_at is None:
                    values['started_at'] = now
                conn.execute(update(scan_jobs_table).where(scan_jobs_table.c.id == row.job_id).values(**values))
            return {'unit_id': row.id, 'job_id': row.job_id, 'directory': row.directory,
                    'options': row.options or {}}

    def set_scan_unit_progress(self, unit_id: int, message: str) -> None:
        with get_engine().begin() as conn:
            conn.execute(update(scan_units_table).where(scan_units_table.c.id == unit_id).values(progress=message))

    def finish_scan_unit(self, unit_id: int, status: str, *, result: Optional[dict],
                         error: Optional[str], summarize: Callable[[list[dict]], str]) -> None:
        """Marks `unit_id` terminal (DONE/FAILED/CANCELLED) and, in the same
        transaction, recomputes its job's status from every one of its
        units (see `_recompute_scan_job_status_on_conn`). `summarize(units)`
        builds the job's final summary text once no unit is left
        QUEUED/RUNNING — injected so this DB-layer method never has to
        import api/tracking.py's ScanSummary/_format_scan_summary (the rest
        of the app only calls into db/ for SQL, never the other way)."""
        now = datetime.now(timezone.utc)
        with get_engine().begin() as conn:
            unit_row = conn.execute(
                select(scan_units_table.c.job_id).where(scan_units_table.c.id == unit_id)
            ).fetchone()
            if unit_row is None:
                return
            conn.execute(
                update(scan_units_table)
                .where(scan_units_table.c.id == unit_id)
                .values(status=status, finished_at=now, result=result, error=error)
            )
            self._recompute_scan_job_status_on_conn(conn, unit_row.job_id, summarize)

    def _recompute_scan_job_status_on_conn(self, conn, job_id: str,
                                           summarize: Callable[[list[dict]], str]) -> None:
        """Job status follows from its units (#137): locks the job row
        first (serialising concurrent consumers finishing two units of the
        same job at once), then — only once no unit is left QUEUED/RUNNING
        (DESELECTED/PLANNED don't count, same as get_scan_jobs) and the job
        hasn't already been finalized (`finished_at` guards that, so this
        is safe to call more than once for the same job) — sets
        `finished_at` + `summary`: status becomes FAILED if any unit FAILED,
        else DONE, UNLESS the job is already CANCELLED, which stays
        CANCELLED (only finished_at/summary get filled in). A PAUSED job
        with units still QUEUED is left untouched (the "no unit
        QUEUED/RUNNING left" condition isn't met)."""
        job_row = conn.execute(
            select(scan_jobs_table.c.status, scan_jobs_table.c.finished_at)
            .where(scan_jobs_table.c.id == job_id)
            .with_for_update()
        ).fetchone()
        if job_row is None or job_row.finished_at is not None:
            return
        units = conn.execute(
            select(scan_units_table.c.status, scan_units_table.c.result)
            .where(scan_units_table.c.job_id == job_id)
        ).fetchall()
        if any(u.status in self._ACTIVE_UNIT_STATUSES for u in units):
            return
        now = datetime.now(timezone.utc)
        summary = summarize([{'status': u.status, 'result': u.result} for u in units])
        new_status = job_row.status
        if job_row.status != 'CANCELLED':
            new_status = 'FAILED' if any(u.status == 'FAILED' for u in units) else 'DONE'
        conn.execute(
            update(scan_jobs_table).where(scan_jobs_table.c.id == job_id)
            .values(status=new_status, finished_at=now, summary=summary)
        )

    def pause_scan_job(self, job_id: str) -> dict:
        with get_engine().begin() as conn:
            row = conn.execute(
                select(scan_jobs_table.c.status).where(scan_jobs_table.c.id == job_id).with_for_update()
            ).fetchone()
            if row is None:
                raise ScanJobNotFoundError(job_id)
            if row.status not in ('QUEUED', 'RUNNING'):
                raise InvalidScanTransitionError(f'Cannot pause a job that is {row.status}')
            conn.execute(update(scan_jobs_table).where(scan_jobs_table.c.id == job_id).values(status='PAUSED'))
        return self.get_scan_job(job_id)

    def resume_scan_job(self, job_id: str, summarize: Callable[[list[dict]], str]) -> dict:
        """PAUSED -> QUEUED; if nothing is actually left to run anymore
        (every unit already terminal), the subsequent recompute inside this
        same transaction takes it straight to DONE/FAILED instead."""
        with get_engine().begin() as conn:
            row = conn.execute(
                select(scan_jobs_table.c.status).where(scan_jobs_table.c.id == job_id).with_for_update()
            ).fetchone()
            if row is None:
                raise ScanJobNotFoundError(job_id)
            if row.status != 'PAUSED':
                raise InvalidScanTransitionError(f'Cannot resume a job that is {row.status}')
            conn.execute(update(scan_jobs_table).where(scan_jobs_table.c.id == job_id).values(status='QUEUED'))
            self._recompute_scan_job_status_on_conn(conn, job_id, summarize)
        return self.get_scan_job(job_id)

    def cancel_scan_job(self, job_id: str, summarize: Callable[[list[dict]], str]) -> dict:
        """All QUEUED/PLANNED units -> CANCELLED, job -> CANCELLED. A unit
        already RUNNING is left as-is here — the caller (api/scanjobs.py)
        flags it in tasks/scanqueue.py's in-memory cancel set right after
        this returns, so its executor notices without a DB write; it'll
        report itself CANCELLED (with its partial result) once it does,
        which is what finally lets the job reach `finished_at`/`summary`
        (set immediately below, in the same transaction, only if nothing is
        RUNNING anymore)."""
        now = datetime.now(timezone.utc)
        with get_engine().begin() as conn:
            # Lock order units -> job, the same order claim_next_scan_unit and
            # finish_scan_unit use; locking the job first could deadlock with
            # a consumer that holds a unit row and waits for the job row.
            conn.execute(
                select(scan_units_table.c.id)
                .where(scan_units_table.c.job_id == job_id,
                       scan_units_table.c.status.in_(('QUEUED', 'PLANNED')))
                .order_by(scan_units_table.c.id)
                .with_for_update()
            ).fetchall()
            row = conn.execute(
                select(scan_jobs_table.c.status).where(scan_jobs_table.c.id == job_id).with_for_update()
            ).fetchone()
            if row is None:
                raise ScanJobNotFoundError(job_id)
            if row.status in self._TERMINAL_JOB_STATUSES:
                raise InvalidScanTransitionError(f'Cannot cancel a job that is {row.status}')
            conn.execute(
                update(scan_units_table)
                .where(scan_units_table.c.job_id == job_id,
                       scan_units_table.c.status.in_(('QUEUED', 'PLANNED')))
                .values(status='CANCELLED', finished_at=now)
            )
            conn.execute(update(scan_jobs_table).where(scan_jobs_table.c.id == job_id).values(status='CANCELLED'))
            self._recompute_scan_job_status_on_conn(conn, job_id, summarize)
        return self.get_scan_job(job_id)

    def delete_scan_job(self, job_id: str) -> None:
        """409 while any unit is RUNNING; otherwise the job (and its units,
        ON DELETE CASCADE) are gone for good."""
        with get_engine().begin() as conn:
            row = conn.execute(select(scan_jobs_table.c.id).where(scan_jobs_table.c.id == job_id)).fetchone()
            if row is None:
                raise ScanJobNotFoundError(job_id)
            running = conn.execute(
                select(func.count()).select_from(scan_units_table)
                .where(scan_units_table.c.job_id == job_id, scan_units_table.c.status == 'RUNNING')
            ).scalar_one()
            if running:
                raise InvalidScanTransitionError('Cannot delete a job while one of its units is running')
            conn.execute(delete(scan_jobs_table).where(scan_jobs_table.c.id == job_id))

    def start_scan_job(self, job_id: str,
                       summarize: Optional[Callable[[list[dict]], str]] = None) -> dict:
        """PLANNED -> QUEUED, same for its PLANNED units — except a unit
        whose directory is already QUEUED/RUNNING in another job, which is
        set CANCELLED with an explanatory error instead of failing the
        whole start (same per-unit `ON CONFLICT` guard as create_scan_job);
        a genuine race that still raises is let through to the caller as a
        plain IntegrityError (mapped to 409 by the API).

        `summarize`, if given (the API always passes `scanqueue.summarize_job`;
        tests that don't care about the 0-unit edge case may omit it), also
        recomputes the job's status in the same transaction right after —
        a no-op for the normal case (some unit is now QUEUED), but the only
        thing that finalizes a #138 plan with zero (non-DESELECTED) units:
        otherwise it would sit QUEUED forever, since no unit ever finishes
        to trigger that recompute."""
        with get_engine().begin() as conn:
            job_row = conn.execute(
                select(scan_jobs_table.c.status).where(scan_jobs_table.c.id == job_id).with_for_update()
            ).fetchone()
            if job_row is None:
                raise ScanJobNotFoundError(job_id)
            if job_row.status != 'PLANNED':
                raise InvalidScanTransitionError(f'Cannot start a job that is {job_row.status}')
            planned_units = conn.execute(
                select(scan_units_table.c.id)
                .where(scan_units_table.c.job_id == job_id, scan_units_table.c.status == 'PLANNED')
            ).fetchall()
            for unit in planned_units:
                try:
                    with conn.begin_nested():
                        conn.execute(
                            update(scan_units_table).where(scan_units_table.c.id == unit.id)
                            .values(status='QUEUED')
                        )
                except IntegrityError:
                    # Another job already has this directory QUEUED/RUNNING
                    # (the partial unique index caught it) — don't fail the
                    # whole start over one unit.
                    conn.execute(
                        update(scan_units_table).where(scan_units_table.c.id == unit.id)
                        .values(status='CANCELLED', error='Already queued in another job',
                                finished_at=datetime.now(timezone.utc))
                    )
            conn.execute(update(scan_jobs_table).where(scan_jobs_table.c.id == job_id).values(status='QUEUED'))
            if summarize is not None:
                self._recompute_scan_job_status_on_conn(conn, job_id, summarize)
        return self.get_scan_job(job_id)

    def cancel_scan_unit(self, job_id: str, unit_id: int, summarize: Callable[[list[dict]], str]) -> bool:
        """Cancels one unit of `job_id`. A QUEUED/PLANNED unit is flipped to
        CANCELLED right away (job recomputed in the same transaction) and
        this returns True. A RUNNING unit is left untouched here — this
        returns False so the caller flags it in tasks/scanqueue.py's
        in-memory cancel set instead, same reasoning as cancel_scan_job."""
        now = datetime.now(timezone.utc)
        with get_engine().begin() as conn:
            row = conn.execute(
                select(scan_units_table.c.status, scan_units_table.c.job_id)
                .where(scan_units_table.c.id == unit_id)
                .with_for_update()
            ).fetchone()
            if row is None or row.job_id != job_id:
                raise ScanUnitNotFoundError(unit_id)
            if row.status == 'RUNNING':
                return False
            if row.status not in ('QUEUED', 'PLANNED'):
                raise InvalidScanTransitionError(f'Cannot cancel a unit that is {row.status}')
            conn.execute(
                update(scan_units_table).where(scan_units_table.c.id == unit_id)
                .values(status='CANCELLED', finished_at=now)
            )
            self._recompute_scan_job_status_on_conn(conn, job_id, summarize)
        return True

    def retry_scan_unit(self, job_id: str, unit_id: int, summarize: Callable[[list[dict]], str]) -> dict:
        """FAILED/CANCELLED -> QUEUED (clearing error/progress/result/
        finished_at); if the job itself was terminal, it goes back to
        QUEUED too (clearing its finished_at/summary). Raises
        DirectoryAlreadyQueuedError (-> 409) if the unit's directory is
        QUEUED/RUNNING in another job right now."""
        with get_engine().begin() as conn:
            row = conn.execute(
                select(scan_units_table.c.status, scan_units_table.c.job_id)
                .where(scan_units_table.c.id == unit_id)
                .with_for_update()
            ).fetchone()
            if row is None or row.job_id != job_id:
                raise ScanUnitNotFoundError(unit_id)
            if row.status not in ('FAILED', 'CANCELLED'):
                raise InvalidScanTransitionError(f'Cannot retry a unit that is {row.status}')
            try:
                with conn.begin_nested():
                    conn.execute(
                        update(scan_units_table)
                        .where(scan_units_table.c.id == unit_id)
                        .values(status='QUEUED', error=None, progress=None, result=None, finished_at=None)
                    )
            except IntegrityError:
                raise DirectoryAlreadyQueuedError(unit_id)
            job_row = conn.execute(
                select(scan_jobs_table.c.status).where(scan_jobs_table.c.id == job_id).with_for_update()
            ).fetchone()
            if job_row.status in self._TERMINAL_JOB_STATUSES:
                conn.execute(
                    update(scan_jobs_table).where(scan_jobs_table.c.id == job_id)
                    .values(status='QUEUED', finished_at=None, summary=None)
                )
        return self.get_scan_job(job_id)

    def move_scan_unit_to_top(self, job_id: str, unit_id: int) -> dict:
        """position = (min position of the job's waiting — PLANNED/QUEUED —
        units) - 1, so it's claimed/started before all of them."""
        with get_engine().begin() as conn:
            row = conn.execute(
                select(scan_units_table.c.status, scan_units_table.c.job_id)
                .where(scan_units_table.c.id == unit_id)
            ).fetchone()
            if row is None or row.job_id != job_id:
                raise ScanUnitNotFoundError(unit_id)
            if row.status not in ('PLANNED', 'QUEUED'):
                raise InvalidScanTransitionError(f'Cannot reorder a unit that is {row.status}')
            min_position = conn.execute(
                select(func.min(scan_units_table.c.position))
                .where(scan_units_table.c.job_id == job_id,
                       scan_units_table.c.status.in_(('PLANNED', 'QUEUED')))
            ).scalar_one()
            conn.execute(
                update(scan_units_table).where(scan_units_table.c.id == unit_id)
                .values(position=min_position - 1)
            )
        return self.get_scan_job(job_id)

    def _transition_scan_unit(self, job_id: str, unit_id: int, from_status: str, to_status: str) -> dict:
        with get_engine().begin() as conn:
            row = conn.execute(
                select(scan_units_table.c.status, scan_units_table.c.job_id)
                .where(scan_units_table.c.id == unit_id)
                .with_for_update()
            ).fetchone()
            if row is None or row.job_id != job_id:
                raise ScanUnitNotFoundError(unit_id)
            if row.status != from_status:
                raise InvalidScanTransitionError(f'Cannot move a unit from {row.status} to {to_status}')
            conn.execute(update(scan_units_table).where(scan_units_table.c.id == unit_id).values(status=to_status))
        return self.get_scan_job(job_id)

    def deselect_scan_unit(self, job_id: str, unit_id: int) -> dict:
        """PLANNED -> DESELECTED (planning only, used by #138)."""
        return self._transition_scan_unit(job_id, unit_id, 'PLANNED', 'DESELECTED')

    def reselect_scan_unit(self, job_id: str, unit_id: int) -> dict:
        """DESELECTED -> PLANNED (planning only, used by #138)."""
        return self._transition_scan_unit(job_id, unit_id, 'DESELECTED', 'PLANNED')

    def recover_scan_queue(self, summarize: Callable[[list[dict]], str]) -> None:
        """Run once at startup, before consumers start (#137): a unit left
        RUNNING by a crash goes back to QUEUED — the executor is idempotent
        (reconciliation is upsert-based, and #136's skip rule makes a
        same-directory rerun cheap) — which in turn means every job left
        RUNNING no longer has a running unit, so it goes back to QUEUED too.

        Also reconciles a CANCELLED job's stray QUEUED/PLANNED units: these
        only exist if the process restarted between a job-level cancel and
        a still-RUNNING unit noticing its in-memory cancel flag (reset to
        QUEUED by the step above, same as any other crashed RUNNING unit) —
        without this, such a unit would never be claimed again (a consumer
        only claims units of a QUEUED/RUNNING job) and its job would never
        reach `finished_at`."""
        now = datetime.now(timezone.utc)
        with get_engine().begin() as conn:
            conn.execute(
                update(scan_units_table).where(scan_units_table.c.status == 'RUNNING')
                .values(status='QUEUED', started_at=None, error=None, progress=None)
            )
            conn.execute(
                update(scan_jobs_table).where(scan_jobs_table.c.status == 'RUNNING')
                .values(status='QUEUED')
            )
            cancelled_job_ids = [r[0] for r in conn.execute(
                select(scan_jobs_table.c.id).where(scan_jobs_table.c.status == 'CANCELLED')
            ).fetchall()]
            for job_id in cancelled_job_ids:
                conn.execute(
                    update(scan_units_table)
                    .where(scan_units_table.c.job_id == job_id,
                           scan_units_table.c.status.in_(('QUEUED', 'PLANNED')))
                    .values(status='CANCELLED', finished_at=now)
                )
                self._recompute_scan_job_status_on_conn(conn, job_id, summarize)
