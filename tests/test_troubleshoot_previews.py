"""Tests for the missing-preview detection + repair (#65):
GET /trouble-shooting/missing-preview and generate_missing_clip_previews
(the POST /trouble-shooting/missing-preview/fix background task). Photo
preview generation shells out to real exiftool/Pillow against a real JPG
from the test footage, mirroring tests/test_tracking_refresh.py. Video
FFprobe failure uses a garbage ".mov" file so FFprobe genuinely returns
None, exercising the real per-file-failure path end to end."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

import api.troubleshoot as troubleshoot
from db.engine import get_engine
from db.models import clip_previews_table, files_table

TEST_PHOTO = Path(__file__).resolve().parent.parent / 'footage' / 'japan_2024' / 'photo' / 'atami' / 'P1011679.JPG'


class _Report:
    def __init__(self):
        self.messages: list[str] = []

    def __call__(self, message: str):
        self.messages.append(message)

    @property
    def last(self) -> str:
        return self.messages[-1] if self.messages else ''


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(troubleshoot.TroubleShootingApi)
    return TestClient(app)


def _insert_file_row(directory: str, file_name: str, md5_hash: str, media_type):
    with get_engine().begin() as conn:
        conn.execute(files_table.insert().values(
            md5_hash=md5_hash, file_name=file_name,
            file_extension=Path(file_name).suffix, media_type=media_type,
            directory=directory,
        ))


def _has_preview(md5_hash: str) -> bool:
    with get_engine().connect() as conn:
        return conn.execute(
            select(clip_previews_table.c.md5_hash).where(clip_previews_table.c.md5_hash == md5_hash)
        ).fetchone() is not None


def test_tracked_photo_without_preview_gets_one_via_repair(db, root_dir):
    assert TEST_PHOTO.exists(), 'test footage missing — see CLAUDE.md "Test footage"'
    photo_path = root_dir / 'photo.jpg'
    import shutil
    shutil.copyfile(TEST_PHOTO, photo_path)

    md5_hash = 'photo-hash-1'
    _insert_file_row(str(root_dir), 'photo.jpg', md5_hash, media_type='photo')
    assert not _has_preview(md5_hash)

    report = _Report()
    troubleshoot.generate_missing_clip_previews(report)

    assert _has_preview(md5_hash)
    assert report.last == 'Generated 1 preview'


def test_non_media_file_does_not_appear_in_missing_preview_listing(db, root_dir):
    client = _make_client()
    _insert_file_row(str(root_dir), 'notes.txt', 'nonmedia-hash', media_type=None)

    resp = client.get('/trouble-shooting/missing-preview')
    assert resp.status_code == 200
    assert resp.json() == []

    files = db.get_files_without_clip_preview({'video', '360_video', 'photo', '360_photo'})
    assert files.empty


def test_ffprobe_failure_for_video_counts_as_failed_and_other_files_still_processed(db, root_dir):
    assert TEST_PHOTO.exists(), 'test footage missing — see CLAUDE.md "Test footage"'

    broken_video = root_dir / 'broken.mov'
    broken_video.write_bytes(b'not actually a video')
    broken_hash = 'video-hash-broken'
    _insert_file_row(str(root_dir), 'broken.mov', broken_hash, media_type='video')

    photo_path = root_dir / 'photo.jpg'
    import shutil
    shutil.copyfile(TEST_PHOTO, photo_path)
    photo_hash = 'photo-hash-2'
    _insert_file_row(str(root_dir), 'photo.jpg', photo_hash, media_type='photo')

    report = _Report()
    troubleshoot.generate_missing_clip_previews(report)

    assert not _has_preview(broken_hash)
    assert _has_preview(photo_hash)
    assert report.last == 'Generated 1 preview · 1 failed'


def test_missing_on_disk_is_counted(db, root_dir):
    gone_hash = 'gone-hash-1'
    _insert_file_row(str(root_dir), 'gone.jpg', gone_hash, media_type='photo')
    # No file ever written to disk -> directory/gone.jpg does not exist.

    report = _Report()
    troubleshoot.generate_missing_clip_previews(report)

    assert not _has_preview(gone_hash)
    assert report.last == 'Generated 0 previews · 1 missing on disk'
