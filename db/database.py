import sqlite3

import pandas as pd

from env.environment import Environment
from ffmpeg.ffmpeg import ClipPreview
from scanner.scanner import ScanResult


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

    def insert_scan_results(self, scan_results: [ScanResult]):
        scan_df = pd.DataFrame([r.model_dump() for r in scan_results])
        self.connect()
        self.__upsert_into_table(scan_df, 'Files')
        self.disconnect()

    def insert_file_details(self, file_details: pd.DataFrame):
        self.connect()
        self.__upsert_into_table(file_details, 'FileDetails')
        self.disconnect()

    def insert_keywords(self, keywords: pd.DataFrame):
        self.connect()
        self.__upsert_into_table(keywords, 'Keywords')
        self.disconnect()

    def insert_clip_preview(self, clip_preview: ClipPreview):
        self.connect()
        df = pd.DataFrame([clip_preview.model_dump()])
        self.__upsert_into_table(df, 'ClipPreviews')
        self.disconnect()

    def __upsert_into_table(self, df: pd.DataFrame, table: str):
        cursor = self._connection.execute(f"PRAGMA table_info({table});")
        sql_columns = [row[1] for row in cursor.fetchall()]
        df = df[sql_columns]
        df.to_sql(f'{table}_temp', self._connection, if_exists='replace', index=False)
        self._connection.execute(f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}_temp')
        self._connection.commit()
        self._connection.execute(f'DROP TABLE IF EXISTS {table}_temp')
        self._connection.commit()
