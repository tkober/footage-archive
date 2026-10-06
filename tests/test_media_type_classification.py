"""Unit tests for `scanner/media_type.py::classify_media_type` (#79) — pure
function, no DB/filesystem involved. See `tests/test_tracking_refresh.py`
for the DB-backed `_probe_and_save` refinement tests, and the footage-backed
tests at the bottom of this file for real Insta360/Lumix files."""

from pathlib import Path

import pytest

from env.environment import Environment
from scanner.media_type import classify_media_type

MEDIA_TYPE_MAP = Environment().get_media_type_map()


def test_insta360_dng_with_arashi_vision_make_is_360_photo():
    assert classify_media_type('.dng', MEDIA_TYPE_MAP, make='Arashi Vision') == '360_photo'


def test_dng_with_other_make_is_plain_photo():
    assert classify_media_type('.dng', MEDIA_TYPE_MAP, make='Panasonic') == 'photo'


def test_dng_with_no_metadata_is_plain_photo():
    assert classify_media_type('.dng', MEDIA_TYPE_MAP) == 'photo'


def test_insp_with_no_metadata_is_360_photo():
    assert classify_media_type('.insp', MEDIA_TYPE_MAP) == '360_photo'


def test_insv_with_no_metadata_is_360_video():
    assert classify_media_type('.insv', MEDIA_TYPE_MAP) == '360_video'


def test_jpg_with_equirectangular_projection_is_360_photo():
    assert classify_media_type('.jpg', MEDIA_TYPE_MAP, projection='equirectangular') == '360_photo'


def test_mp4_with_equirectangular_spherical_metadata_is_360_video():
    assert classify_media_type('.mp4', MEDIA_TYPE_MAP, projection='equirectangular') == '360_video'


def test_mp4_with_insta360_make_but_no_projection_stays_video():
    """Make alone is never enough for video — an Insta360 camera's
    reframed/flat MP4 export must stay 'video', not '360_video'."""
    assert classify_media_type('.mp4', MEDIA_TYPE_MAP, make='Arashi Vision') == 'video'


def test_unrecognised_extension_is_none():
    assert classify_media_type('.xyz', MEDIA_TYPE_MAP) is None


def test_extension_case_insensitive():
    assert classify_media_type('.DNG', MEDIA_TYPE_MAP, make='Arashi Vision') == '360_photo'


# ---------------------------------------------------------------------------
# Footage-backed (real exiftool, real files) — skipped if the test footage
# isn't present. See CLAUDE.md "Test footage"; note these paths are
# footage/footage/japan_2024/... (not footage/japan_2024 — the pre-existing
# tests that use the latter are a known, unrelated issue).
# ---------------------------------------------------------------------------

_NAGASAKI = Path(__file__).resolve().parent.parent / 'footage' / 'footage' / 'japan_2024' / '360' / 'nagasaki'
_ATAMI_PHOTO = Path(__file__).resolve().parent.parent / 'footage' / 'footage' / 'japan_2024' / 'photo' / 'atami'

_nagasaki_dng = next(iter(sorted(_NAGASAKI.glob('*.dng'))), None) if _NAGASAKI.is_dir() else None
_atami_rw2 = next(iter(sorted(_ATAMI_PHOTO.glob('*.RW2'))), None) if _ATAMI_PHOTO.is_dir() else None
_atami_jpg = next(iter(sorted(_ATAMI_PHOTO.glob('*.JPG'))), None) if _ATAMI_PHOTO.is_dir() else None


@pytest.mark.skipif(_nagasaki_dng is None, reason='test footage missing — see CLAUDE.md "Test footage"')
def test_nagasaki_insta360_dng_classifies_as_360_photo():
    from photos.exif import probe_photo
    probe = probe_photo(md5_hash='x', file_path=str(_nagasaki_dng))
    assert probe is not None
    assert probe.camera_make == 'Arashi Vision'
    result = classify_media_type('.dng', MEDIA_TYPE_MAP, make=probe.camera_make, projection=probe.projection)
    assert result == '360_photo'


@pytest.mark.skipif(_atami_rw2 is None, reason='test footage missing — see CLAUDE.md "Test footage"')
def test_atami_lumix_rw2_classifies_as_plain_photo():
    from photos.exif import probe_photo
    probe = probe_photo(md5_hash='x', file_path=str(_atami_rw2))
    assert probe is not None
    assert probe.camera_make != 'Arashi Vision'
    result = classify_media_type('.rw2', MEDIA_TYPE_MAP, make=probe.camera_make, projection=probe.projection)
    assert result == 'photo'


@pytest.mark.skipif(_atami_jpg is None, reason='test footage missing — see CLAUDE.md "Test footage"')
def test_atami_lumix_jpg_classifies_as_plain_photo():
    from photos.exif import probe_photo
    probe = probe_photo(md5_hash='x', file_path=str(_atami_jpg))
    assert probe is not None
    result = classify_media_type('.jpg', MEDIA_TYPE_MAP, make=probe.camera_make, projection=probe.projection)
    assert result == 'photo'
