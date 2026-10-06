"""Tests for the preview-status feature (#77): recording outcomes in
PreviewStatus, the derived `preview_status` surfaced by the directory
listing + file-details API, the repair's include_failed toggle, cascade
deletion of PreviewStatus rows, and the "currently generating" registry.
Mirrors tests/test_troubleshoot_previews.py's shelling-out-to-real-tools
pattern (Pillow for photos, a garbage file for FFprobe failures)."""

import shutil
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import api.troubleshoot as troubleshoot
from api.files import FilesApi
from db.database import Database
from db.engine import get_engine
from db.models import files_table, preview_status_table
from tasks.preview_registry import discard, is_pending, pending_previews



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
    app.include_router(FilesApi)
    app.include_router(troubleshoot.TroubleShootingApi)
    return TestClient(app)


def _insert_file_row(directory: str, file_name: str, md5_hash: str, media_type='photo'):
    with get_engine().begin() as conn:
        conn.execute(files_table.insert().values(
            md5_hash=md5_hash, file_name=file_name,
            file_extension=Path(file_name).suffix, media_type=media_type,
            directory=directory,
        ))


def _make_tiny_jpeg(path: Path):
    img = Image.new('RGB', (40, 30), (120, 60, 30))
    img.save(path, format='JPEG')


def _get_preview_status_row(md5_hash: str):
    with get_engine().connect() as conn:
        from sqlalchemy import select
        row = conn.execute(
            select(preview_status_table).where(preview_status_table.c.md5_hash == md5_hash)
        ).fetchone()
    return row._asdict() if row is not None else None


# ------------------------------------------------------------------
# Recording outcomes
# ------------------------------------------------------------------

def test_successful_photo_preview_records_ok(db, root_dir):
    photo_path = root_dir / 'ok.jpg'
    _make_tiny_jpeg(photo_path)
    md5_hash = 'ok-hash'
    _insert_file_row(str(root_dir), 'ok.jpg', md5_hash, media_type='photo')

    report = _Report()
    troubleshoot.generate_missing_clip_previews(report)

    status = _get_preview_status_row(md5_hash)
    assert status is not None
    assert status['status'] == 'ok'
    assert status['reason'] is None
    assert status['attempted_at'] is not None


def test_corrupt_photo_with_image_extension_records_failed(db, root_dir):
    # A valid JPEG header (so PIL identifies the format and `generate_preview`
    # takes the non-unsupported path) but truncated body, so decoding fails
    # with a generic OSError once PIL is forced to actually read the pixel
    # data (on resize/save) -> recorded as 'failed', not 'unsupported'.
    import io
    buf = io.BytesIO()
    Image.new('RGB', (200, 150), (10, 20, 30)).save(buf, format='JPEG')
    full_bytes = buf.getvalue()
    corrupt_path = root_dir / 'corrupt.jpg'
    corrupt_path.write_bytes(full_bytes[:len(full_bytes) // 3])
    md5_hash = 'corrupt-hash'
    _insert_file_row(str(root_dir), 'corrupt.jpg', md5_hash, media_type='photo')

    report = _Report()
    troubleshoot.generate_missing_clip_previews(report)

    status = _get_preview_status_row(md5_hash)
    assert status is not None
    assert status['status'] == 'failed'
    assert status['reason']


def test_unidentifiable_format_records_unsupported(db, root_dir):
    # A .heic PIL can't open at all (no RAW_EXTENSIONS entry either, #78, so
    # it never goes through rawpy) -> PIL.UnidentifiedImageError -> 'unsupported'.
    heic_path = root_dir / 'weird.heic'
    heic_path.write_bytes(b'totally not an image PIL can identify')
    md5_hash = 'heic-hash'
    _insert_file_row(str(root_dir), 'weird.heic', md5_hash, media_type='360_photo')

    report = _Report()
    troubleshoot.generate_missing_clip_previews(report)

    status = _get_preview_status_row(md5_hash)
    assert status is not None
    assert status['status'] == 'unsupported'
    assert '.heic' in status['reason']


def test_dng_is_raw_extension_records_failed_not_unsupported(db, root_dir):
    # #78: .dng is now a RAW_EXTENSIONS entry sent through rawpy (e.g. an
    # Insta360 photo), never Pillow — a garbage .dng is 'failed', not
    # 'unsupported', unlike before #78.
    dng_path = root_dir / 'weird.dng'
    dng_path.write_bytes(b'totally not a dng rawpy can decode')
    md5_hash = 'dng-hash'
    _insert_file_row(str(root_dir), 'weird.dng', md5_hash, media_type='360_photo')

    report = _Report()
    troubleshoot.generate_missing_clip_previews(report)

    status = _get_preview_status_row(md5_hash)
    assert status is not None
    assert status['status'] == 'failed'
    assert status['reason']


def test_corrupt_file_with_pillow_extension_records_failed_not_unsupported(db, root_dir):
    # Pillow handles .jpg, so an unidentifiable .jpg is a broken file, not an
    # unsupported format.
    jpg_path = root_dir / 'broken.jpg'
    jpg_path.write_bytes(b'garbage')
    md5_hash = 'broken-jpg-hash'
    _insert_file_row(str(root_dir), 'broken.jpg', md5_hash)

    troubleshoot.generate_missing_clip_previews(_Report())

    status = _get_preview_status_row(md5_hash)
    assert status is not None
    assert status['status'] == 'failed'


def test_normal_scan_records_ffprobe_failure(db, root_dir):
    # _probe_and_save returns before generate_preview when FFprobe fails —
    # the scan path must still record 'failed' instead of leaving 'missing'.
    from datetime import datetime
    from api.tracking import _probe_and_save
    from scanner.scanner import ScanResult

    (root_dir / 'scan_broken.mp4').write_bytes(b'not a video')
    md5_hash = 'scan-broken-video-hash'
    _insert_file_row(str(root_dir), 'scan_broken.mp4', md5_hash, media_type='video')
    sc = ScanResult(md5_hash=md5_hash, file_name='scan_broken.mp4', file_extension='.mp4',
                    media_type='video', directory=str(root_dir), last_indexed_at=datetime.now())

    with pending_previews([md5_hash]):
        assert _probe_and_save(sc, Database(), generate_clip_preview=True) is False
        assert not is_pending(md5_hash)

    status = _get_preview_status_row(md5_hash)
    assert status is not None
    assert status['status'] == 'failed'
    assert 'FFprobe' in status['reason']


def test_video_with_no_frames_or_ffprobe_failure_records_failed(db, root_dir):
    broken_video = root_dir / 'broken.mov'
    broken_video.write_bytes(b'not actually a video')
    md5_hash = 'broken-video-hash'
    _insert_file_row(str(root_dir), 'broken.mov', md5_hash, media_type='video')

    report = _Report()
    troubleshoot.generate_missing_clip_previews(report)

    status = _get_preview_status_row(md5_hash)
    assert status is not None
    assert status['status'] == 'failed'
    assert 'FFprobe' in status['reason']


# ------------------------------------------------------------------
# Derived preview_status in the directory listing + file details
# ------------------------------------------------------------------

def test_directory_listing_reports_missing_failed_unsupported_ok(db, root_dir):
    client = _make_client()

    ok_path = root_dir / 'ok.jpg'
    _make_tiny_jpeg(ok_path)
    ok_hash = 'dir-ok-hash'
    _insert_file_row(str(root_dir), 'ok.jpg', ok_hash, media_type='photo')
    Database().insert_raw_preview(ok_hash, b'fake-jpeg-bytes')

    failed_path = root_dir / 'bad.jpg'
    failed_path.write_bytes(b'garbage')
    failed_hash = 'dir-failed-hash'
    _insert_file_row(str(root_dir), 'bad.jpg', failed_hash, media_type='photo')
    Database().set_preview_status(failed_hash, 'failed', 'boom')

    unsupported_path = root_dir / 'weird.dng'
    unsupported_path.write_bytes(b'garbage')
    unsupported_hash = 'dir-unsupported-hash'
    _insert_file_row(str(root_dir), 'weird.dng', unsupported_hash, media_type='360_photo')
    Database().set_preview_status(unsupported_hash, 'unsupported', 'nope (.dng)')

    missing_path = root_dir / 'never.jpg'
    missing_path.write_bytes(b'garbage')
    missing_hash = 'dir-missing-hash'
    _insert_file_row(str(root_dir), 'never.jpg', missing_hash, media_type='photo')

    resp = client.post('/files/directory', json={'path': str(root_dir)})
    assert resp.status_code == 200, resp.text
    items = {item['name']: item for item in resp.json()['items']}

    assert items['ok.jpg']['preview_status'] == 'ok'
    assert items['bad.jpg']['preview_status'] == 'failed'
    assert items['weird.dng']['preview_status'] == 'unsupported'
    assert items['never.jpg']['preview_status'] == 'missing'


def test_directory_listing_reports_generating_from_registry(db, root_dir):
    client = _make_client()

    pending_path = root_dir / 'pending.jpg'
    pending_path.write_bytes(b'garbage')
    pending_hash = 'dir-pending-hash'
    _insert_file_row(str(root_dir), 'pending.jpg', pending_hash, media_type='photo')

    with pending_previews([pending_hash]):
        resp = client.post('/files/directory', json={'path': str(root_dir)})
        assert resp.status_code == 200, resp.text
        items = {item['name']: item for item in resp.json()['items']}
        assert items['pending.jpg']['preview_status'] == 'generating'

    assert not is_pending(pending_hash)


def test_untracked_and_non_media_files_have_no_preview_status(db, root_dir):
    client = _make_client()

    (root_dir / 'untracked.jpg').write_bytes(b'x')
    text_hash = 'text-hash'
    (root_dir / 'notes.txt').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'notes.txt', text_hash, media_type=None)

    resp = client.post('/files/directory', json={'path': str(root_dir)})
    assert resp.status_code == 200, resp.text
    items = {item['name']: item for item in resp.json()['items']}

    assert items['untracked.jpg']['preview_status'] is None
    assert items['notes.txt']['preview_status'] is None


def test_file_details_reports_preview_status_and_error(db, root_dir):
    client = _make_client()

    bad_path = root_dir / 'bad.jpg'
    bad_path.write_bytes(b'garbage')
    bad_hash = 'details-failed-hash'
    _insert_file_row(str(root_dir), 'bad.jpg', bad_hash, media_type='photo')
    Database().set_preview_status(bad_hash, 'failed', 'some reason')

    resp = client.get('/files/details', params={'path': str(bad_path)})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['preview_status'] == 'failed'
    assert data['preview_error'] == 'some reason'
    assert data['preview_attempted_at'] is not None


# ------------------------------------------------------------------
# Repair: default skips failed/unsupported, include_failed retries them
# ------------------------------------------------------------------

def test_repair_default_skips_failed_and_unsupported(db, root_dir):
    failed_path = root_dir / 'bad.jpg'
    failed_path.write_bytes(b'garbage')
    failed_hash = 'repair-failed-hash'
    _insert_file_row(str(root_dir), 'bad.jpg', failed_hash, media_type='photo')
    Database().set_preview_status(failed_hash, 'failed', 'boom')

    never_path = root_dir / 'never.jpg'
    _make_tiny_jpeg(never_path)
    never_hash = 'repair-never-hash'
    _insert_file_row(str(root_dir), 'never.jpg', never_hash, media_type='photo')

    report = _Report()
    troubleshoot.generate_missing_clip_previews(report, include_failed=False)

    # The never-attempted file got a preview; the already-failed one was
    # left alone (its PreviewStatus row/reason is unchanged).
    assert report.last == 'Generated 1 preview'
    status = _get_preview_status_row(failed_hash)
    assert status['reason'] == 'boom'


def test_repair_include_failed_retries_them(db, root_dir):
    # Fix the underlying file so a retry actually succeeds this time.
    bad_path = root_dir / 'bad.jpg'
    bad_path.write_bytes(b'garbage')
    bad_hash = 'retry-hash'
    _insert_file_row(str(root_dir), 'bad.jpg', bad_hash, media_type='photo')
    Database().set_preview_status(bad_hash, 'failed', 'boom')

    report = _Report()
    troubleshoot.generate_missing_clip_previews(report, include_failed=False)
    assert report.last == 'Generated 0 previews'  # still broken, not retried by default

    _make_tiny_jpeg(bad_path)  # now fix the file on disk
    report2 = _Report()
    troubleshoot.generate_missing_clip_previews(report2, include_failed=True)
    assert report2.last == 'Generated 1 preview'

    status = _get_preview_status_row(bad_hash)
    assert status['status'] == 'ok'


def test_missing_preview_listing_distinguishes_statuses(db, root_dir):
    client = _make_client()

    failed_path = root_dir / 'bad.jpg'
    failed_path.write_bytes(b'garbage')
    failed_hash = 'listing-failed-hash'
    _insert_file_row(str(root_dir), 'bad.jpg', failed_hash, media_type='photo')
    Database().set_preview_status(failed_hash, 'failed', 'boom')

    never_path = root_dir / 'never.jpg'
    never_path.write_bytes(b'garbage')
    never_hash = 'listing-never-hash'
    _insert_file_row(str(root_dir), 'never.jpg', never_hash, media_type='photo')

    resp = client.get('/trouble-shooting/missing-preview')
    assert resp.status_code == 200, resp.text
    rows = {row['md5_hash']: row for row in resp.json()}

    assert rows[failed_hash]['preview_status'] == 'failed'
    assert rows[failed_hash]['reason'] == 'boom'
    assert rows[never_hash]['preview_status'] == 'missing'
    assert rows[never_hash]['reason'] is None


# ------------------------------------------------------------------
# Cascade delete
# ------------------------------------------------------------------

def test_delete_files_removes_preview_status_row(db, root_dir):
    md5_hash = 'delete-hash'
    _insert_file_row(str(root_dir), 'gone.jpg', md5_hash, media_type='photo')
    Database().set_preview_status(md5_hash, 'failed', 'boom')
    assert _get_preview_status_row(md5_hash) is not None

    removed = Database().delete_files([md5_hash])
    assert removed == 1
    assert _get_preview_status_row(md5_hash) is None


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

def test_pending_previews_context_manager_adds_and_removes():
    assert not is_pending('reg-a')
    assert not is_pending('reg-b')
    with pending_previews(['reg-a', 'reg-b']):
        assert is_pending('reg-a')
        assert is_pending('reg-b')
    assert not is_pending('reg-a')
    assert not is_pending('reg-b')


def test_discard_removes_a_single_hash_early():
    with pending_previews(['reg-x', 'reg-y']):
        assert is_pending('reg-x')
        discard('reg-x')
        assert not is_pending('reg-x')
        assert is_pending('reg-y')


def test_discard_is_safe_when_hash_not_registered():
    discard('never-registered')  # must not raise
