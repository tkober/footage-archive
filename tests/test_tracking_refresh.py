"""Tests for POST /tracking/refresh (#64) — `refresh_tracked_files`:
rescan already-tracked files by hash (re-probe + regenerate preview) without
re-hashing. Photo probing/thumbnailing shells out to real exiftool/Pillow
against a real JPG from the test footage — no mocking needed there. Video
probing uses a garbage ".mov" file so FFprobe genuinely returns None,
exercising the real "without preview" path end to end."""

import shutil
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

import api.tracking as tracking
import scanner.scanner as scanner_module
from api.dtos import RefreshQuery
from db.engine import get_engine
from db.models import (
    clip_previews_table,
    file_details_table,
    file_keywords_table,
    files_table,
    keywords_table,
    list_items_table,
    lists_table,
    locations_table,
)
from scanner.scanner import ScanResult

TEST_PHOTO = Path(__file__).resolve().parent.parent / 'footage' / 'japan_2024' / 'photo' / 'atami' / 'P1011679.JPG'


class _Report:
    def __init__(self):
        self.messages: list[str] = []

    def __call__(self, message: str):
        self.messages.append(message)

    @property
    def last(self) -> str:
        return self.messages[-1] if self.messages else ''


def _track(db, md5_hash: str, path: Path, media_type: str | None):
    sc = ScanResult(
        md5_hash=md5_hash,
        file_name=path.name,
        file_extension=path.suffix,
        media_type=media_type,
        directory=str(path.parent),
        last_indexed_at=datetime.now(),
    )
    db.insert_scan_results([sc])


def _has_preview(md5_hash: str) -> bool:
    with get_engine().connect() as conn:
        return conn.execute(
            select(clip_previews_table.c.md5_hash).where(clip_previews_table.c.md5_hash == md5_hash)
        ).fetchone() is not None


def test_refresh_photo_without_preview_gets_one(db, root_dir):
    assert TEST_PHOTO.exists(), 'test footage missing — see CLAUDE.md "Test footage"'
    photo_path = root_dir / 'photo.jpg'
    shutil.copyfile(TEST_PHOTO, photo_path)

    md5_hash = 'photo-hash-1'
    _track(db, md5_hash, photo_path, media_type='photo')
    assert not _has_preview(md5_hash)

    report = _Report()
    tracking.refresh_tracked_files(RefreshQuery(md5_hashes=[md5_hash]), report)

    assert _has_preview(md5_hash)
    assert report.last == 'Rescanned 1 file'


def test_refresh_unknown_hash_is_skipped(db, root_dir):
    report = _Report()
    tracking.refresh_tracked_files(RefreshQuery(md5_hashes=['does-not-exist']), report)

    assert report.last == 'Rescanned 0 files'


def test_refresh_missing_file_counts_as_missing(db, root_dir):
    gone_path = root_dir / 'gone.jpg'
    # Track it, but never actually create the file on disk.
    md5_hash = 'missing-hash-1'
    _track(db, md5_hash, gone_path, media_type='photo')

    report = _Report()
    tracking.refresh_tracked_files(RefreshQuery(md5_hashes=[md5_hash]), report)

    assert report.last == 'Rescanned 0 files · 1 missing'


def test_refresh_does_not_rehash(db, root_dir, monkeypatch):
    """Scanner is monkeypatched to blow up if used — refresh must build its
    ScanResult straight from the existing Files row (md5_hash, media_type,
    path), never from re-hashing the file."""
    def _boom(*args, **kwargs):
        raise AssertionError('Scanner must not be invoked by a rescan (#64)')

    monkeypatch.setattr(scanner_module.Scanner, 'scan_files', _boom)
    monkeypatch.setattr(scanner_module.Scanner, 'scan_directory', _boom)
    monkeypatch.setattr(scanner_module.Scanner, 'md5_hash', _boom)

    photo_path = root_dir / 'photo.jpg'
    shutil.copyfile(TEST_PHOTO, photo_path)
    md5_hash = 'photo-hash-2'
    _track(db, md5_hash, photo_path, media_type='photo')

    report = _Report()
    tracking.refresh_tracked_files(RefreshQuery(md5_hashes=[md5_hash]), report)

    assert _has_preview(md5_hash)
    assert report.last == 'Rescanned 1 file'


def test_refresh_video_ffprobe_failure_counts_without_preview_and_isolates_error(db, root_dir):
    """A garbage ".mov" file makes the real FFprobe come back with no
    'format' key -> None, so _probe_and_save returns False. This must be
    reported as "without preview", not as a failure, and must not raise."""
    video_path = root_dir / 'broken.mov'
    video_path.write_bytes(b'not actually a video')
    md5_hash = 'video-hash-1'
    _track(db, md5_hash, video_path, media_type='video')

    report = _Report()
    tracking.refresh_tracked_files(RefreshQuery(md5_hashes=[md5_hash]), report)

    assert not _has_preview(md5_hash)
    assert report.last == 'Rescanned 1 file · 1 without preview'


def test_refresh_leaves_keywords_location_and_lists_unchanged(db, root_dir):
    photo_path = root_dir / 'photo.jpg'
    shutil.copyfile(TEST_PHOTO, photo_path)
    md5_hash = 'photo-hash-3'
    _track(db, md5_hash, photo_path, media_type='photo')

    with get_engine().begin() as conn:
        location_id = conn.execute(
            locations_table.insert().values(name='Atami', city='Atami').returning(locations_table.c.id)
        ).scalar()
        conn.execute(file_details_table.insert().values(md5_hash=md5_hash, location_id=location_id))
        kw_id = conn.execute(
            keywords_table.insert().values(keyword='beach').returning(keywords_table.c.id)
        ).scalar()
        conn.execute(file_keywords_table.insert().values(md5_hash=md5_hash, keyword_id=kw_id))
        list_id = conn.execute(
            lists_table.insert().values(name='Favorites').returning(lists_table.c.id)
        ).scalar()
        conn.execute(list_items_table.insert().values(list_id=list_id, md5_hash=md5_hash, item_code='ABC123'))

    report = _Report()
    tracking.refresh_tracked_files(RefreshQuery(md5_hashes=[md5_hash]), report)

    with get_engine().connect() as conn:
        assert conn.execute(
            select(file_details_table.c.location_id).where(file_details_table.c.md5_hash == md5_hash)
        ).scalar() == location_id
        assert conn.execute(
            select(keywords_table.c.keyword)
            .join(file_keywords_table, keywords_table.c.id == file_keywords_table.c.keyword_id)
            .where(file_keywords_table.c.md5_hash == md5_hash)
        ).scalar() == 'beach'
        assert conn.execute(
            select(list_items_table.c.item_code)
            .where(list_items_table.c.md5_hash == md5_hash)
        ).scalar() == 'ABC123'


def test_refresh_endpoint_rejects_empty_hashes():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(tracking.TrackingApi)
    client = TestClient(app)

    resp = client.post('/tracking/refresh', json={'md5_hashes': []})
    assert resp.status_code == 400
