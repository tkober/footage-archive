import uuid
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import case, delete, func, select, update

from db.engine import get_engine, upsert, upsert_ignore
from db.models import (
    clip_previews_table,
    file_details_table,
    file_keywords_table,
    files_table,
    keywords_table,
    locations_table,
    photo_details_table,
    video_details_table,
)
from ffmpeg.ffmpeg import ClipPreview
from scanner.scanner import ScanResult


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
            select(files_table.c.file_name, files_table.c.md5_hash, files_table.c.media_type)
            .where(files_table.c.directory == directory)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {row[0]: {'md5_hash': row[1], 'media_type': row[2]} for row in rows}

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

    _CLUSTER_CELL_SIZES = [20.0, 20.0, 20.0, 20.0, 8.0, 8.0, 3.0, 3.0, 1.0, 1.0, 0.3, 0.3, 0.05, 0.05]

    def get_map_points(self, west: float, south: float, east: float, north: float,
                       zoom: int) -> list[dict]:
        coalesce_lat = func.coalesce(locations_table.c.latitude, file_details_table.c.latitude)
        coalesce_lon = func.coalesce(locations_table.c.longitude, file_details_table.c.longitude)

        base_stmt = (
            select(
                files_table.c.md5_hash,
                files_table.c.file_name,
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

        if zoom >= 14:
            with get_engine().connect() as conn:
                rows = conn.execute(base_stmt).fetchall()
            result = []
            for row in rows:
                r = row._asdict()
                is_video = r['media_type'] in ('video', '360_video')
                result.append({
                    'latitude': r['lat'], 'longitude': r['lon'],
                    'count': 1,
                    'video_count': 1 if is_video else 0,
                    'photo_count': 0 if is_video else 1,
                    'md5_hash': r['md5_hash'],
                    'file_name': r['file_name'],
                    'media_type': r['media_type'],
                })
            return result

        cell = self._CLUSTER_CELL_SIZES[min(zoom, len(self._CLUSTER_CELL_SIZES) - 1)]
        subq = base_stmt.subquery()
        lat_cluster = (func.round(subq.c.lat / cell) * cell).label('latitude')
        lon_cluster = (func.round(subq.c.lon / cell) * cell).label('longitude')
        is_video_expr = subq.c.media_type.in_(['video', '360_video'])

        cluster_stmt = (
            select(
                lat_cluster,
                lon_cluster,
                func.count().label('count'),
                func.sum(case((is_video_expr, 1), else_=0)).label('video_count'),
                func.sum(case((~is_video_expr, 1), else_=0)).label('photo_count'),
            )
            .select_from(subq)
            .group_by(lat_cluster, lon_cluster)
        )

        with get_engine().connect() as conn:
            rows = conn.execute(cluster_stmt).fetchall()
        return [
            {**row._asdict(), 'md5_hash': None, 'file_name': None, 'media_type': None}
            for row in rows
        ]

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

        count_stmt = select(func.count()).select_from(base_from)
        data_stmt = (
            select(
                files_table.c.md5_hash, files_table.c.file_name,
                files_table.c.directory, files_table.c.media_type,
                file_details_table.c.recorded_at,
                locations_table.c.country, locations_table.c.city,
            )
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
