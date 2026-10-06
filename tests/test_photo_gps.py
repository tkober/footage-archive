"""Unit tests for #91: an Insta360 X3 photo taken without a GPS fix writes
empty/'undef' GPS tags (.insp) or an all-zero fix (.dng) instead of omitting
them. `probe_photo` must not crash on the .insp case (the bare `float('')`
it used to do raised outside the try block, so the file got no PhotoDetails
at all) and must treat 0/0 as "no fix" rather than storing a map point at
"Null Island" in the Atlantic. exiftool is stubbed via `run_niced`, same
pattern as `tests/test_media_type_probe.py` / `tests/test_ffmpeg_command.py`
— no real files touched."""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

import photos.exif as exif_module
from photos.exif import _parse_gps, probe_photo
from scanner.scanner import ScanResult

FOOTAGE_ROOT = Path(__file__).resolve().parent.parent / 'footage' / 'footage' / 'japan_2024'
NAGASAKI_DNG = FOOTAGE_ROOT / '360' / 'nagasaki' / 'IMG_20241031_161144_00_001.dng'
NAGASAKI_INSP = FOOTAGE_ROOT / '360' / 'nagasaki' / 'IMG_20241031_161144_00_001.insp'


def _stub_exiftool(monkeypatch, tags: dict):
    data = {'Make': 'Arashi Vision', **tags}

    def fake_run(cmd, **kwargs):
        class Result:
            stdout = json.dumps([data])
        return Result()

    monkeypatch.setattr(exif_module, 'run_niced', fake_run)


def test_insp_empty_gps_strings_and_undef_altitude_dont_crash(monkeypatch):
    """The real bug (#91): .insp without a fix writes GPSLatitude/GPSLongitude
    as '' and GPSAltitude as 'undef'. The old code's bare `float(lat)` raised
    a ValueError outside the try block, so probe_photo never returned at all."""
    _stub_exiftool(monkeypatch, {
        'GPSLatitude': '', 'GPSLatitudeRef': '', 'GPSLongitude': '', 'GPSLongitudeRef': '',
        'GPSAltitude': 'undef', 'GPSAltitudeRef': 'undef',
    })

    probe = probe_photo('hash', '/fake/path.insp')

    assert probe is not None
    assert probe.latitude is None
    assert probe.longitude is None
    assert probe.altitude is None
    assert probe.camera_make == 'Arashi Vision'


def test_dng_zero_zero_with_refs_is_treated_as_no_fix(monkeypatch):
    """The .dng case: a real numeric 0/0/0 fix with valid N/E refs — still no
    real position (Null Island), so it must come back as None too."""
    _stub_exiftool(monkeypatch, {
        'GPSLatitude': 0, 'GPSLatitudeRef': 'N', 'GPSLongitude': 0, 'GPSLongitudeRef': 'E',
        'GPSAltitude': 0, 'GPSAltitudeRef': 0,
    })

    probe = probe_photo('hash', '/fake/path.dng')

    assert probe.latitude is None
    assert probe.longitude is None
    assert probe.altitude is None


def test_parse_gps_valid_north_east_rounds_and_keeps_sign():
    data = {
        'GPSLatitude': 35.123456789, 'GPSLatitudeRef': 'N',
        'GPSLongitude': 139.987654321, 'GPSLongitudeRef': 'E',
        'GPSAltitude': 12.34, 'GPSAltitudeRef': 0,
    }
    lat, lon, alt = _parse_gps(data)
    assert lat == pytest.approx(35.123457)
    assert lon == pytest.approx(139.987654)
    assert alt == pytest.approx(12.3)


def test_parse_gps_south_west_with_below_sea_level_altitude_is_negative():
    data = {
        'GPSLatitude': 35.0, 'GPSLatitudeRef': 'S',
        'GPSLongitude': 139.0, 'GPSLongitudeRef': 'W',
        'GPSAltitude': 10.0, 'GPSAltitudeRef': 1,
    }
    lat, lon, alt = _parse_gps(data)
    assert lat == -35.0
    assert lon == -139.0
    assert alt == -10.0


def test_parse_gps_non_numeric_garbage_is_none():
    data = {
        'GPSLatitude': 'garbage', 'GPSLatitudeRef': 'N',
        'GPSLongitude': 'also garbage', 'GPSLongitudeRef': 'E',
        'GPSAltitude': 'nope', 'GPSAltitudeRef': 0,
    }
    lat, lon, alt = _parse_gps(data)
    assert (lat, lon, alt) == (None, None, None)


def test_parse_gps_lat_zero_lon_nonzero_is_kept():
    """Equator crossing a real country (e.g. Ecuador) — lat 0 alone is a
    legitimate position, only lat==lon==0 together means 'no fix'."""
    data = {
        'GPSLatitude': 0, 'GPSLatitudeRef': 'N',
        'GPSLongitude': 78.5, 'GPSLongitudeRef': 'W',
        'GPSAltitude': 0, 'GPSAltitudeRef': 0,
    }
    lat, lon, alt = _parse_gps(data)
    assert lat == 0.0
    assert lon == -78.5


def test_parse_gps_missing_lon_drops_lone_lat():
    data = {'GPSLatitude': 35.0, 'GPSLatitudeRef': 'N'}
    assert _parse_gps(data) == (None, None, None)


# ---------------------------------------------------------------------------
# Footage-backed (real exiftool) — skipped if the test footage isn't present.
# ---------------------------------------------------------------------------

def test_nagasaki_insp_and_dng_without_gps_fix_probe_to_none():
    if not NAGASAKI_INSP.exists() or not NAGASAKI_DNG.exists():
        pytest.skip('test footage missing — see CLAUDE.md "Test footage"')

    for path in (NAGASAKI_INSP, NAGASAKI_DNG):
        probe = probe_photo('hash', str(path))
        assert probe is not None, f'{path} failed to probe'
        assert probe.latitude is None, f'{path}: {probe.latitude}'
        assert probe.longitude is None, f'{path}: {probe.longitude}'
        assert probe.altitude is None, f'{path}: {probe.altitude}'


# ---------------------------------------------------------------------------
# DB-backed: a stray pre-migration 0/0 FileDetails row (e.g. inserted before
# this fix shipped) must not surface on the map or the detail panel.
# ---------------------------------------------------------------------------

def test_null_island_row_excluded_from_map_points_and_file_gps(db, root_dir):
    path = root_dir / 'null_island.dng'
    path.write_bytes(b'not a real dng')
    md5_hash = 'null-island-hash'

    db.insert_scan_results([ScanResult(
        md5_hash=md5_hash, file_name=path.name, file_extension=path.suffix,
        media_type='360_photo', directory=str(path.parent), last_indexed_at=datetime.now(),
    )])
    db.insert_file_details(pd.DataFrame([{
        'md5_hash': md5_hash, 'latitude': 0.0, 'longitude': 0.0, 'altitude': 0.0,
    }]))

    assert db.get_file_gps(md5_hash) is None
    points = db.get_map_points(west=-180, south=-90, east=180, north=90, zoom=1)
    assert all(p['md5_hash'] != md5_hash for p in points)
