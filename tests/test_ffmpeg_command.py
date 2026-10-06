"""Unit tests for the shared ffmpeg frame-extraction command (#71): bounded
decoding threads instead of ffmpeg's one-thread-per-core default, cheap
keyframe-only decoding, and an accurate fallback when that yields nothing.
Also covers the aspect-ratio-preserving scale+pad filter (#80)."""

import io
from pathlib import Path

import pytest
from PIL import Image

from ffmpeg import ffmpeg
from ffmpeg.ffmpeg import FFmpeg, FFprobe, FFmpegInput, _build_frame_command, _extract_frame


def test_build_frame_command_bounds_threads_and_skips_non_keyframes(monkeypatch):
    monkeypatch.setenv('FFMPEG_THREADS', '3')
    command = _build_frame_command('/videos/clip.mov', '00:00:05', 320, 180, 'out.jpeg')

    assert command.count('-threads') == 2
    for i, token in enumerate(command):
        if token == '-threads':
            assert command[i + 1] == '3'
    assert command[command.index('-skip_frame') + 1] == 'nokey'
    # -skip_frame is a decoder (input) option, so it must precede -i
    assert command.index('-skip_frame') < command.index('-i')
    assert command[command.index('-ss') + 1] == '00:00:05'
    assert command[command.index('-i') + 1] == '/videos/clip.mov'
    assert command[-1] == 'out.jpeg'


def test_build_frame_command_accurate_variant_decodes_all_frames():
    command = _build_frame_command('/videos/clip.mov', '00:00:05', 320, 180, 'out.jpeg',
                                   keyframes_only=False)
    assert '-skip_frame' not in command


def test_build_frame_command_scales_keeping_aspect_and_pads_to_box(monkeypatch):
    """#80: the old command stretched every frame to a hard WxH with a
    plain `scale=W:H`, distorting non-16:9 sources (portrait phone video,
    360 dual-fisheye). The new filter keeps the source aspect ratio, pads
    with black to land on the same WxH box, and normalizes SAR."""
    command = _build_frame_command('/videos/clip.mov', '00:00:05', 320, 180, 'out.jpeg')
    vf = command[command.index('-vf') + 1]
    assert vf == (
        'scale=320:180:force_original_aspect_ratio=decrease:force_divisible_by=2,'
        'pad=320:180:(ow-iw)/2:(oh-ih)/2:color=black,'
        'setsar=1'
    )
    # autorotate must stay on (default) so a rotated portrait phone video is
    # already upright before scale/pad runs
    assert '-noautorotate' not in command


def test_extract_frame_falls_back_to_accurate_decode_when_keyframe_pass_yields_nothing(monkeypatch, tmp_path):
    out_file = tmp_path / 'frame.jpeg'
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if '-skip_frame' not in cmd:  # only the accurate pass produces a frame
            Path(cmd[-1]).write_bytes(b'jpeg')

    monkeypatch.setattr(ffmpeg, 'run_niced', fake_run)
    _extract_frame('/videos/clip.mov', '00:00:55', 320, 180, str(out_file))

    assert len(calls) == 2
    assert '-skip_frame' in calls[0] and '-skip_frame' not in calls[1]
    assert out_file.exists()


def test_extract_frame_skips_fallback_when_keyframe_pass_succeeds(monkeypatch, tmp_path):
    out_file = tmp_path / 'frame.jpeg'
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b'jpeg')

    monkeypatch.setattr(ffmpeg, 'run_niced', fake_run)
    _extract_frame('/videos/clip.mov', '00:00:05', 320, 180, str(out_file))

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Footage-backed (real ffmpeg/ffprobe) — skipped if the test footage isn't
# present. See CLAUDE.md "Test footage"; note these paths are
# footage/footage/japan_2024/... (not footage/japan_2024 — the pre-existing
# tests that use the latter are a known, unrelated issue, see
# tests/test_media_type_classification.py).
# ---------------------------------------------------------------------------

_PHONE_PORTRAIT = (Path(__file__).resolve().parent.parent / 'footage' / 'footage' / 'japan_2024'
                   / 'phone' / 'kokura' / 'video' / '20241030_122706.mp4')
_NARA_DIR = Path(__file__).resolve().parent.parent / 'footage' / 'footage' / 'japan_2024' / 'video' / 'nara'
_nara_mov = next(iter(sorted(_NARA_DIR.glob('*.MOV'))), None) if _NARA_DIR.is_dir() else None


def _clip_preview_for(md5_hash: str, file_path: Path):
    probe = FFprobe().probe_file(md5_hash, str(file_path))
    assert probe is not None
    video = FFmpegInput(md5_hash=probe.md5_hash, file_path=probe.file_path, duration=probe.duration)
    return FFmpeg(f'test_{md5_hash}').generate_clip_preview(video)


def _mean_rgb(image: Image.Image, box) -> tuple[float, float, float]:
    cropped = image.crop(box)
    pixels = list(cropped.getdata())
    n = len(pixels)
    return (sum(p[0] for p in pixels) / n, sum(p[1] for p in pixels) / n, sum(p[2] for p in pixels) / n)


@pytest.mark.skipif(not _PHONE_PORTRAIT.is_file(), reason='test footage missing — see CLAUDE.md "Test footage"')
def test_portrait_phone_video_preview_is_pillarboxed_not_distorted(tmp_path):
    """20241030_122706.mp4 is 3840x2160 with a -90 degree rotation display
    matrix, so ffmpeg's autorotate renders it portrait. #80: the frame
    must be pillarboxed (black bars on the left/right, content in the
    middle) rather than squashed to fill the full 320x180 box."""
    clip = _clip_preview_for('portrait_test', _PHONE_PORTRAIT)
    assert clip is not None
    assert clip.frame_height == 180
    assert clip.frame_width == 320
    assert clip.overall_height == 180
    assert clip.overall_width == 320 * 5 + clip.padding * 4

    image = Image.open(io.BytesIO(clip.data))
    first_frame = image.crop((0, 0, 320, 180))

    left_edge = _mean_rgb(first_frame, (0, 0, 10, 180))
    right_edge = _mean_rgb(first_frame, (310, 0, 320, 180))
    centre = _mean_rgb(first_frame, (150, 0, 170, 180))

    # Pillarboxed: near-black bars at the edges, real (brighter) content
    # in the middle.
    assert max(left_edge) < 20
    assert max(right_edge) < 20
    assert max(centre) > max(left_edge) + 15


@pytest.mark.skipif(_nara_mov is None, reason='test footage missing — see CLAUDE.md "Test footage"')
def test_16_9_lumix_video_preview_has_no_black_bars():
    """A 16:9 Lumix MOV already matches the box's aspect ratio, so scale+pad
    should produce no padding at all (#80) — same as the old plain scale."""
    clip = _clip_preview_for('nara_test', _nara_mov)
    assert clip is not None
    assert clip.frame_height == 180
    assert clip.frame_width == 320

    image = Image.open(io.BytesIO(clip.data))
    first_frame = image.crop((0, 0, 320, 180))

    left_edge = _mean_rgb(first_frame, (0, 0, 5, 180))
    right_edge = _mean_rgb(first_frame, (315, 0, 320, 180))
    top_edge = _mean_rgb(first_frame, (0, 0, 320, 5))
    bottom_edge = _mean_rgb(first_frame, (0, 175, 320, 180))

    # None of the edges should be pure black padding — a 16:9 source fills
    # the whole 320x180 box.
    assert max(left_edge) > 20 or max(right_edge) > 20 or max(top_edge) > 20 or max(bottom_edge) > 20
