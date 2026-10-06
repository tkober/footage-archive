"""Tests for RAW thumbnail/full-image generation (#78): every extension in
`RAW_EXTENSIONS` is sent through rawpy — never Pillow — so a RAW file is
never recorded 'unsupported'; and the Insta360 (Arashi Vision) correction
(auto WB instead of camera WB + fisheyes side by side) is applied only for that
make. Pure-unit tests below monkeypatch rawpy/exiftool so they don't need
real footage; the two at the bottom use the real Nagasaki DNG + atami RW2
and are skipped if that footage isn't present (see CLAUDE.md "Test footage")."""

import io
import time
from pathlib import Path

import numpy as np
import pytest
import rawpy
from PIL import Image

from photos import exif as exif_module
from photos.exif import RAW_EXTENSIONS, generate_photo_thumbnail

FOOTAGE_ROOT = Path(__file__).resolve().parent.parent / 'footage' / 'footage' / 'japan_2024'
NAGASAKI_DNG = FOOTAGE_ROOT / '360' / 'nagasaki' / 'IMG_20241031_161144_00_001.dng'
NAGASAKI_INSP = FOOTAGE_ROOT / '360' / 'nagasaki' / 'IMG_20241031_161144_00_001.insp'
ATAMI_RW2 = FOOTAGE_ROOT / 'photo' / 'atami' / 'P1011679.RW2'


def _fake_rgb(width=64, height=64) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


# ---------------------------------------------------------------------------
# RAW extension routing: any RAW_EXTENSIONS file is 'failed' on error, never
# 'unsupported' — the opposite of a non-RAW extension Pillow can't open.
# ---------------------------------------------------------------------------

def test_garbage_dng_is_failed_not_unsupported(tmp_path):
    assert '.dng' in RAW_EXTENSIONS
    bad = tmp_path / 'garbage.dng'
    bad.write_bytes(b'not actually a dng')

    data, status, reason = generate_photo_thumbnail('h', str(bad))

    assert data is None
    assert status == 'failed'
    assert reason


def test_garbage_non_raw_extension_is_unsupported(tmp_path):
    bad = tmp_path / 'garbage.xyz'
    bad.write_bytes(b'not an image')

    data, status, reason = generate_photo_thumbnail('h', str(bad))

    assert data is None
    assert status == 'unsupported'


# ---------------------------------------------------------------------------
# Insta360 rule: Arashi Vision → use_auto_wb + fisheyes side by side; every other make
# keeps use_camera_wb and no rearranging. Mock exiftool's Make lookup and
# rawpy.imread so no real footage is needed.
# ---------------------------------------------------------------------------

class _FakeRawPostprocess:
    def __init__(self, rgb: np.ndarray):
        self._rgb = rgb

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def postprocess(self, **kwargs):
        self.postprocess_kwargs = kwargs
        return self._rgb

    def extract_thumb(self):
        raise rawpy.LibRawNoThumbnailError('no thumb')


def test_insta360_make_gets_auto_wb_and_fisheyes_side_by_side(tmp_path, monkeypatch):
    dng = tmp_path / 'fake.dng'
    dng.write_bytes(b'x')

    monkeypatch.setattr(exif_module, '_camera_make', lambda path: 'Arashi Vision')
    # Portrait raw (height > width), like a real Insta360 DNG: red top
    # fisheye, blue bottom fisheye.
    rgb = _fake_rgb(width=40, height=80)
    rgb[:40] = (255, 0, 0)
    rgb[40:] = (0, 0, 255)
    fake = _FakeRawPostprocess(rgb)
    monkeypatch.setattr(rawpy, 'imread', lambda path: fake)

    img = exif_module._postprocess_raw(str(dng), half_size=True)

    assert fake.postprocess_kwargs['use_auto_wb'] is True
    assert 'use_camera_wb' not in fake.postprocess_kwargs
    # Stacked fisheyes (40x80) → side by side (80x40), top half on the left.
    assert img.width == 80
    assert img.height == 40
    assert img.getpixel((0, 0)) == (255, 0, 0)
    assert img.getpixel((40, 0)) == (0, 0, 255)


def test_non_insta360_make_keeps_camera_wb_unchanged_layout(tmp_path, monkeypatch):
    rw2 = tmp_path / 'fake.rw2'
    rw2.write_bytes(b'x')

    monkeypatch.setattr(exif_module, '_camera_make', lambda path: 'Panasonic')
    fake = _FakeRawPostprocess(_fake_rgb(width=40, height=80))
    monkeypatch.setattr(rawpy, 'imread', lambda path: fake)

    img = exif_module._postprocess_raw(str(rw2), half_size=True)

    assert fake.postprocess_kwargs['use_camera_wb'] is True
    assert 'use_auto_wb' not in fake.postprocess_kwargs
    # Not rearranged — stays portrait.
    assert img.width == 40
    assert img.height == 80


# ---------------------------------------------------------------------------
# Embedded-thumb preference: a wide-enough embedded JPEG thumb is used as-is,
# never falling through to a full postprocess.
# ---------------------------------------------------------------------------

class _FakeThumb:
    def __init__(self, data: bytes):
        self.format = rawpy.ThumbFormat.JPEG
        self.data = data


class _FakeRawWithThumb:
    def __init__(self, thumb_jpeg: bytes):
        self._thumb = _FakeThumb(thumb_jpeg)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_thumb(self):
        return self._thumb

    def postprocess(self, **kwargs):
        raise AssertionError('postprocess should not be called when the embedded thumb is wide enough')


def _jpeg_bytes(width, height) -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', (width, height), 'red').save(buf, format='JPEG')
    return buf.getvalue()


def test_embedded_thumb_preferred_when_wide_enough(tmp_path, monkeypatch):
    rw2 = tmp_path / 'fake.rw2'
    rw2.write_bytes(b'x')

    thumb_jpeg = _jpeg_bytes(1920, 1440)
    monkeypatch.setattr(rawpy, 'imread', lambda path: _FakeRawWithThumb(thumb_jpeg))

    data, status, reason = generate_photo_thumbnail('h', str(rw2), max_width=600)

    assert status == 'ok'
    img = Image.open(io.BytesIO(data))
    assert img.width == 600  # resized down from the 1920-wide embedded thumb


def test_embedded_thumb_too_small_falls_through_to_postprocess(tmp_path, monkeypatch):
    rw2 = tmp_path / 'fake.rw2'
    rw2.write_bytes(b'x')

    class _FakeRawSmallThumb(_FakeRawWithThumb):
        def postprocess(self, **kwargs):
            return _fake_rgb(width=800, height=600)

    thumb_jpeg = _jpeg_bytes(100, 75)  # narrower than max_width=600
    monkeypatch.setattr(rawpy, 'imread', lambda path: _FakeRawSmallThumb(thumb_jpeg))
    monkeypatch.setattr(exif_module, '_camera_make', lambda path: 'Panasonic')

    data, status, reason = generate_photo_thumbnail('h', str(rw2), max_width=600)

    assert status == 'ok'
    img = Image.open(io.BytesIO(data))
    assert img.width == 600


# ---------------------------------------------------------------------------
# Footage-backed: real Nagasaki DNG + atami RW2.
# ---------------------------------------------------------------------------

def test_nagasaki_dng_thumbnail_is_landscape_2to1():
    if not NAGASAKI_DNG.exists():
        pytest.skip('test footage missing — see CLAUDE.md "Test footage"')

    start = time.time()
    data, status, reason = generate_photo_thumbnail('h', str(NAGASAKI_DNG), max_width=600)
    elapsed = time.time() - start

    assert status == 'ok', reason
    img = Image.open(io.BytesIO(data))
    assert img.width == 600
    ratio = img.width / img.height
    assert 1.8 < ratio < 2.2, f'expected ~2:1 landscape, got {img.width}x{img.height}'
    assert elapsed < 15, f'DNG thumbnail took {elapsed:.1f}s, expected a few seconds'


def test_atami_rw2_thumbnail_uses_embedded_preview(monkeypatch):
    if not ATAMI_RW2.exists():
        pytest.skip('test footage missing — see CLAUDE.md "Test footage"')

    called = {'postprocess': False}
    real_postprocess = exif_module._postprocess_raw

    def spy(*args, **kwargs):
        called['postprocess'] = True
        return real_postprocess(*args, **kwargs)

    monkeypatch.setattr(exif_module, '_postprocess_raw', spy)

    start = time.time()
    data, status, reason = generate_photo_thumbnail('h', str(ATAMI_RW2), max_width=600)
    elapsed = time.time() - start

    assert status == 'ok', reason
    img = Image.open(io.BytesIO(data))
    assert img.width == 600
    assert not called['postprocess'], 'RW2 has an embedded preview — postprocess should not run'
    assert elapsed < 5, f'RW2 thumbnail (embedded preview) took {elapsed:.1f}s, expected well under a second'
