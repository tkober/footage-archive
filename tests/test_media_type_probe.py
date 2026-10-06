"""DB-backed tests for #79: `api/tracking.py::_probe_and_save` refining a
tracked file's `media_type` from real probe metadata instead of trusting
whatever extension-based value is already stored in `Files` — so a
previously misclassified file gets corrected on its next scan/rescan.
`probe_photo` is monkeypatched (the point under test is the refinement
logic, not exiftool itself — that's covered by
`tests/test_media_type_classification.py`'s footage-backed tests)."""

from datetime import datetime
from pathlib import Path

import api.tracking as tracking
import photos.exif as exif_module
from db.database import Database
from scanner.scanner import ScanResult


def _track(db: Database, md5_hash: str, path: Path, media_type: str) -> ScanResult:
    sc = ScanResult(
        md5_hash=md5_hash,
        file_name=path.name,
        file_extension=path.suffix,
        media_type=media_type,
        directory=str(path.parent),
        last_indexed_at=datetime.now(),
    )
    db.insert_scan_results([sc])
    return sc


def test_dng_stored_as_360_photo_is_downgraded_to_photo_on_other_make(db, root_dir, monkeypatch):
    """A .dng previously classified 360_photo (e.g. by an older extension
    map that lumped .dng in with .insp) gets corrected to 'photo' once its
    probed EXIF Make says it isn't an Insta360 file."""
    path = root_dir / 'other_camera.dng'
    path.write_bytes(b'not a real dng, probe_photo is monkeypatched')

    md5_hash = 'dng-other-make'
    sc = _track(db, md5_hash, path, media_type='360_photo')

    fake_probe = exif_module.PhotoProbeResult(
        md5_hash=md5_hash, file_path=str(path), camera_make='Panasonic', projection=None,
    )
    monkeypatch.setattr(tracking, 'probe_photo', lambda md5_hash, file_path: fake_probe)

    result = tracking._probe_and_save(sc, db, generate_clip_preview=False)

    assert result is False
    assert sc.media_type == 'photo'
    assert db.get_file_by_hash(md5_hash)['media_type'] == 'photo'


def test_dng_stored_as_photo_is_upgraded_to_360_photo_on_insta360_make(db, root_dir, monkeypatch):
    """The reverse: a .dng stored as plain 'photo' gets upgraded to
    '360_photo' once probed metadata shows it's an Insta360 file."""
    path = root_dir / 'insta360.dng'
    path.write_bytes(b'not a real dng, probe_photo is monkeypatched')

    md5_hash = 'dng-insta360-make'
    sc = _track(db, md5_hash, path, media_type='photo')

    fake_probe = exif_module.PhotoProbeResult(
        md5_hash=md5_hash, file_path=str(path), camera_make='Arashi Vision', projection=None,
    )
    monkeypatch.setattr(tracking, 'probe_photo', lambda md5_hash, file_path: fake_probe)

    result = tracking._probe_and_save(sc, db, generate_clip_preview=False)

    assert result is False
    assert sc.media_type == '360_photo'
    assert db.get_file_by_hash(md5_hash)['media_type'] == '360_photo'


def test_matching_classification_leaves_media_type_untouched(db, root_dir, monkeypatch):
    """When the refined classification agrees with what's stored,
    Database.set_media_type is never called (no spurious write)."""
    path = root_dir / 'other_camera2.dng'
    path.write_bytes(b'not a real dng, probe_photo is monkeypatched')

    md5_hash = 'dng-no-change'
    sc = _track(db, md5_hash, path, media_type='photo')

    fake_probe = exif_module.PhotoProbeResult(
        md5_hash=md5_hash, file_path=str(path), camera_make='Panasonic', projection=None,
    )
    monkeypatch.setattr(tracking, 'probe_photo', lambda md5_hash, file_path: fake_probe)

    calls = []
    monkeypatch.setattr(Database, 'set_media_type', lambda self, h, mt: calls.append((h, mt)))

    tracking._probe_and_save(sc, db, generate_clip_preview=False)

    assert calls == []
    assert sc.media_type == 'photo'
