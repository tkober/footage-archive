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

    def insert_keywords(self, keywords: pd.DataFrame, identifier: str = generate_identifier()):
        self.connect()
        self.__upsert_into_table(keywords, 'Keywords', temp_table_suffix=identifier)
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
        self._connection.execute(f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}_{temp_table_suffix}')
        self._connection.commit()
        self._connection.execute(f'DROP TABLE IF EXISTS {table}_{temp_table_suffix}')
        self._connection.commit()

    def get_tracked_filenames_in_directory(self, directory: str) -> set:
        self.connect()
        cursor = self._connection.execute(
            'SELECT file_name FROM Files WHERE directory = ?', (directory,)
        )
        result = {row[0] for row in cursor.fetchall()}
        self.disconnect()
        return result

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

    def get_files_without_clip_preview(self) -> pd.DataFrame:
        self.connect()
        with open("sql/missing_clip_previews.sql", "r") as file:
            sql_query = file.read()

        result = pd.read_sql_query(sql_query, self._connection)
        self.disconnect()

        return result
