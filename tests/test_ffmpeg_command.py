"""Unit tests for the shared ffmpeg frame-extraction command (#71): bounded
decoding threads instead of ffmpeg's one-thread-per-core default, cheap
keyframe-only decoding, and an accurate fallback when that yields nothing."""

from pathlib import Path

from ffmpeg import ffmpeg
from ffmpeg.ffmpeg import _build_frame_command, _extract_frame


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
