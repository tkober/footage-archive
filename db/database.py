import sqlite3
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd

from env.environment import Environment
from ffmpeg.ffmpeg import ClipPreview
from scanner.scanner import ScanResult


def generate_identifier():
    return str(uuid.uuid4()).replace('-', '')


class Database:
    _env: Environment
    _connection: sqlite3.Connection = None

    def __init__(self):
        self._env = Environment()

    def connect(self) -> 'Database':
        if self._connection is None:
            self._connection = sqlite3.connect(self._env.get_db_path())

        return self

    def migrate(self) -> 'Database':
        cursor = self._connection.execute("PRAGMA table_info(Keywords)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'md5_hash' in columns:
            self._connection.executescript('''
                CREATE TABLE IF NOT EXISTS Keywords_new (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT UNIQUE NOT NULL
                );
                INSERT OR IGNORE INTO Keywords_new (keyword) SELECT DISTINCT keyword FROM Keywords;
                CREATE TABLE IF NOT EXISTS FileKeywords (
                    md5_hash   TEXT,
                    keyword_id INTEGER REFERENCES Keywords_new (id),
                    PRIMARY KEY (md5_hash, keyword_id)
                );
                INSERT OR IGNORE INTO FileKeywords (md5_hash, keyword_id)
                    SELECT k.md5_hash, kn.id FROM Keywords k
                    JOIN Keywords_new kn ON k.keyword = kn.keyword;
                DROP TABLE Keywords;
                ALTER TABLE Keywords_new RENAME TO Keywords;
            ''')
            self._connection.commit()
        return self

    def setup(self) -> 'Database':
        with open('sql/setup.sql', 'r') as f:
            script = f.read()
            cursor = self._connection.cursor()
            cursor.executescript(script)
            self._connection.commit()

        return self

    def disconnect(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def insert_scan_results(self, scan_results: [ScanResult], identifier: str = generate_identifier()) -> ScanResult:
        scan_df = pd.DataFrame([r.model_dump() for r in scan_results])
        self.connect()
        self.__upsert_into_table(scan_df, 'Files', temp_table_suffix=identifier)
        self.disconnect()

    def insert_file_details(self, details: pd.DataFrame, identifier: str = generate_identifier()):
        """Upserts into FileDetails and VideoDetails based on the columns present in the dataframe."""
        self.connect()
        self.__upsert_into_table(details, 'FileDetails', temp_table_suffix=identifier)
        self.__upsert_into_table(details, 'VideoDetails', temp_table_suffix=identifier + '_v')
        self.disconnect()

    def insert_video_details(self, details: pd.DataFrame, identifier: str = generate_identifier()):
        self.connect()
        self.__upsert_into_table(details, 'VideoDetails', temp_table_suffix=identifier)
        self.disconnect()

    def insert_photo_details(self, details: pd.DataFrame, identifier: str = generate_identifier()):
        self.connect()
        self.__upsert_into_table(details, 'PhotoDetails', temp_table_suffix=identifier)
        self.disconnect()

    def insert_keywords(self, keywords: pd.DataFrame, identifier: str = generate_identifier()):
        self.connect()
        for _, row in keywords[['md5_hash', 'keyword']].iterrows():
            self._connection.execute(
                'INSERT OR IGNORE INTO Keywords (keyword) VALUES (?)', (row['keyword'],))
            self._connection.execute(
                'INSERT OR IGNORE INTO FileKeywords (md5_hash, keyword_id) '
                'SELECT ?, id FROM Keywords WHERE keyword = ?',
                (row['md5_hash'], row['keyword']))
        self._connection.commit()
        self.disconnect()

    def insert_raw_preview(self, md5_hash: str, data: bytes, identifier: str = generate_identifier()):
        self.connect()
        df = pd.DataFrame([{'md5_hash': md5_hash, 'data': data}])
        self.__upsert_into_table(df, 'ClipPreviews', temp_table_suffix=identifier)
        self.disconnect()

    def insert_clip_preview(self, clip_preview: ClipPreview, identifier: str = generate_identifier()):
        self.connect()
        df = pd.DataFrame([clip_preview.model_dump()])
        self.__upsert_into_table(df, 'ClipPreviews', temp_table_suffix=identifier)
        self.disconnect()

    def __upsert_into_table(self, df: pd.DataFrame, table: str, temp_table_suffix: str = 'temp'):
        cursor = self._connection.execute(f"PRAGMA table_info({table});")
        sql_columns = [row[1] for row in cursor.fetchall()]
        available = [c for c in sql_columns if c in df.columns]
        if not available or 'md5_hash' not in available:
            return
        df = df[available]
        df.to_sql(f'{table}_{temp_table_suffix}', self._connection, if_exists='replace', index=False)
        cols = ', '.join(available)
        self._connection.execute(f'INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM {table}_{temp_table_suffix}')
        self._connection.commit()
        self._connection.execute(f'DROP TABLE IF EXISTS {table}_{temp_table_suffix}')
        self._connection.commit()

    def get_tracked_files_in_directory(self, directory: str) -> dict:
        self.connect()
        cursor = self._connection.execute(
            'SELECT file_name, md5_hash, media_type FROM Files WHERE directory = ?', (directory,)
        )
        result = {row[0]: {'md5_hash': row[1], 'media_type': row[2]} for row in cursor.fetchall()}
        self.disconnect()
        return result

    def get_file_by_hash(self, md5_hash: str) -> Optional[dict]:
        self.connect()
        cursor = self._connection.execute(
            'SELECT md5_hash, file_name, file_extension, media_type, directory, last_indexed_at '
            'FROM Files WHERE md5_hash = ?', (md5_hash,)
        )
        row = cursor.fetchone()
        self.disconnect()
        if row is None:
            return None
        return {
            'md5_hash': row[0],
            'file_name': row[1],
            'file_extension': row[2],
            'media_type': row[3],
            'directory': row[4],
            'last_indexed_at': row[5],
        }

    def get_file_by_path(self, file_path: str) -> Optional[dict]:
        p = Path(file_path)
        self.connect()
        cursor = self._connection.execute(
            'SELECT md5_hash, file_name, file_extension, media_type, directory, last_indexed_at '
            'FROM Files WHERE directory = ? AND file_name = ?',
            (str(p.parent), p.name)
        )
        row = cursor.fetchone()
        self.disconnect()
        if row is None:
            return None
        return {
            'md5_hash': row[0],
            'file_name': row[1],
            'file_extension': row[2],
            'media_type': row[3],
            'directory': row[4],
            'last_indexed_at': row[5],
        }

    def get_video_details(self, md5_hash: str) -> Optional[dict]:
        self.connect()
        cursor = self._connection.execute(
            'SELECT width, height, frame_rate, frame_rate_verbose, video_codec, bit_depth, '
            'audio_codec, audio_bit_depth, audio_sample_rate, audio_channels, duration_tc '
            'FROM VideoDetails WHERE md5_hash = ?', (md5_hash,)
        )
        row = cursor.fetchone()
        self.disconnect()
        if row is None:
            return None
        keys = ['width', 'height', 'frame_rate', 'frame_rate_verbose', 'video_codec', 'bit_depth',
                'audio_codec', 'audio_bit_depth', 'audio_sample_rate', 'audio_channels', 'duration_tc']
        return dict(zip(keys, row))

    def get_photo_details(self, md5_hash: str) -> Optional[dict]:
        self.connect()
        cursor = self._connection.execute(
            'SELECT width, height, camera_make, camera_model, iso, aperture, shutter_speed, '
            'focal_length, color_space, bit_depth FROM PhotoDetails WHERE md5_hash = ?', (md5_hash,)
        )
        row = cursor.fetchone()
        self.disconnect()
        if row is None:
            return None
        keys = ['width', 'height', 'camera_make', 'camera_model', 'iso', 'aperture',
                'shutter_speed', 'focal_length', 'color_space', 'bit_depth']
        return dict(zip(keys, row))

    def rename_file(self, md5_hash: str, new_file_name: str):
        self.connect()
        self._connection.execute(
            'UPDATE Files SET file_name = ? WHERE md5_hash = ?',
            (new_file_name, md5_hash)
        )
        self._connection.commit()
        self.disconnect()

    def get_keywords(self, md5_hash: str) -> list[str]:
        self.connect()
        cursor = self._connection.execute(
            'SELECT k.keyword FROM Keywords k '
            'JOIN FileKeywords fk ON k.id = fk.keyword_id '
            'WHERE fk.md5_hash = ? ORDER BY k.keyword ASC', (md5_hash,))
        rows = cursor.fetchall()
        self.disconnect()
        return [row[0] for row in rows]

    def add_keyword(self, md5_hash: str, keyword: str) -> None:
        self.connect()
        self._connection.execute(
            'INSERT OR IGNORE INTO Keywords (keyword) VALUES (?)', (keyword,))
        self._connection.execute(
            'INSERT OR IGNORE INTO FileKeywords (md5_hash, keyword_id) '
            'SELECT ?, id FROM Keywords WHERE keyword = ?', (md5_hash, keyword))
        self._connection.commit()
        self.disconnect()

    def delete_keyword(self, md5_hash: str, keyword: str) -> None:
        self.connect()
        self._connection.execute(
            'DELETE FROM FileKeywords WHERE md5_hash = ? '
            'AND keyword_id = (SELECT id FROM Keywords WHERE keyword = ?)',
            (md5_hash, keyword))
        self._connection.commit()
        self.disconnect()

    def get_file_gps(self, md5_hash: str) -> tuple[float, float] | None:
        self.connect()
        cursor = self._connection.execute(
            'SELECT latitude, longitude FROM FileDetails WHERE md5_hash = ?', (md5_hash,)
        )
        row = cursor.fetchone()
        self.disconnect()
        if row and row[0] is not None and row[1] is not None:
            return (row[0], row[1])
        return None

    _CLUSTER_CELL_SIZES = [20.0, 20.0, 20.0, 20.0, 8.0, 8.0, 3.0, 3.0, 1.0, 1.0, 0.3, 0.3, 0.05, 0.05]

    def get_map_points(self, west: float, south: float, east: float, north: float,
                       zoom: int) -> list[dict]:
        self.connect()
        base_sql = '''
            SELECT f.md5_hash, f.file_name, f.media_type,
                   COALESCE(l.latitude,  fd.latitude)  AS lat,
                   COALESCE(l.longitude, fd.longitude) AS lon
            FROM Files f
            JOIN FileDetails fd ON f.md5_hash = fd.md5_hash
            LEFT JOIN Locations l ON fd.location_id = l.id
            WHERE COALESCE(l.latitude,  fd.latitude)  IS NOT NULL
              AND COALESCE(l.longitude, fd.longitude) IS NOT NULL
              AND COALESCE(l.latitude,  fd.latitude)  BETWEEN ? AND ?
              AND COALESCE(l.longitude, fd.longitude) BETWEEN ? AND ?
        '''
        params = (south, north, west, east)

        if zoom >= 14:
            cursor = self._connection.execute(base_sql, params)
            rows = cursor.fetchall()
            self.disconnect()
            keys = ['md5_hash', 'file_name', 'media_type', 'lat', 'lon']
            result = []
            for row in rows:
                r = dict(zip(keys, row))
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
        cluster_sql = f'''
            SELECT ROUND(lat / {cell}) * {cell} AS latitude,
                   ROUND(lon / {cell}) * {cell} AS longitude,
                   COUNT(*) AS count,
                   SUM(CASE WHEN media_type IN ('video', '360_video') THEN 1 ELSE 0 END) AS video_count,
                   SUM(CASE WHEN media_type NOT IN ('video', '360_video') THEN 1 ELSE 0 END) AS photo_count
            FROM ({base_sql})
            GROUP BY latitude, longitude
        '''
        cursor = self._connection.execute(cluster_sql, params)
        rows = cursor.fetchall()
        self.disconnect()
        keys = ['latitude', 'longitude', 'count', 'video_count', 'photo_count']
        return [
            {**dict(zip(keys, row)), 'md5_hash': None, 'file_name': None, 'media_type': None}
            for row in rows
        ]

    def get_all_keywords(self) -> list[str]:
        self.connect()
        cursor = self._connection.execute(
            'SELECT keyword FROM Keywords ORDER BY keyword ASC')
        rows = cursor.fetchall()
        self.disconnect()
        return [row[0] for row in rows]

    def get_all_locations(self) -> list[dict]:
        self.connect()
        cursor = self._connection.execute(
            'SELECT id, name, city, region, country, latitude, longitude '
            'FROM Locations ORDER BY country, city, name'
        )
        rows = cursor.fetchall()
        self.disconnect()
        keys = ['id', 'name', 'city', 'region', 'country', 'latitude', 'longitude']
        return [dict(zip(keys, row)) for row in rows]

    def create_location(self, name: Optional[str], city: Optional[str], region: Optional[str],
                        country: Optional[str], latitude: Optional[float], longitude: Optional[float]) -> int:
        self.connect()
        cursor = self._connection.execute(
            'INSERT INTO Locations (name, city, region, country, latitude, longitude) VALUES (?, ?, ?, ?, ?, ?)',
            (name, city, region, country, latitude, longitude)
        )
        self._connection.commit()
        row_id = cursor.lastrowid
        self.disconnect()
        return row_id

    def get_location_for_file(self, md5_hash: str) -> Optional[dict]:
        self.connect()
        cursor = self._connection.execute(
            'SELECT l.id, l.name, l.city, l.region, l.country, l.latitude, l.longitude '
            'FROM Locations l JOIN FileDetails fd ON l.id = fd.location_id '
            'WHERE fd.md5_hash = ?', (md5_hash,)
        )
        row = cursor.fetchone()
        self.disconnect()
        if row is None:
            return None
        keys = ['id', 'name', 'city', 'region', 'country', 'latitude', 'longitude']
        return dict(zip(keys, row))

    def assign_location(self, md5_hash: str, location_id: Optional[int]) -> None:
        self.connect()
        self._connection.execute(
            'INSERT INTO FileDetails (md5_hash, location_id) VALUES (?, ?) '
            'ON CONFLICT(md5_hash) DO UPDATE SET location_id = excluded.location_id',
            (md5_hash, location_id)
        )
        self._connection.commit()
        self.disconnect()

    def get_clip_preview(self, md5_hash: str) -> bytes | None:
        self.connect()
        cursor = self._connection.execute(
            'SELECT data FROM ClipPreviews WHERE md5_hash = ?', (md5_hash,)
        )
        row = cursor.fetchone()
        self.disconnect()
        return row[0] if row else None

    def get_files_without_clip_preview(self) -> pd.DataFrame:
        self.connect()
        with open("sql/missing_clip_previews.sql", "r") as file:
            sql_query = file.read()

        result = pd.read_sql_query(sql_query, self._connection)
        self.disconnect()

        return result
