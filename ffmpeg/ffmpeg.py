import logging
import math
import re
import io
import subprocess
import json
from pathlib import Path
from PIL import Image
from pydantic import BaseModel

from env.environment import Environment
from tasks.loadcontrol import heavy_slot, run_niced


def _seconds_to_tc(seconds: int) -> str:
    hh = seconds // 3600
    mm = (seconds % 3600) // 60
    ss = seconds % 60
    return f'{hh:02}:{mm:02}:{ss:02}:00'


def _eval_frame_rate(fraction: str) -> float | None:
    try:
        num, den = fraction.split('/')
        return round(int(num) / int(den), 3)
    except Exception:
        return None


class FFmpegInput(BaseModel):
    md5_hash: str
    file_path: str
    duration: int

    @staticmethod
    def from_time_code(md5_hash: str, file_path: str, duration_tc: str) -> "FFmpegInput":
        pattern = r'([0-9]{2})(:)([0-9]{2})(:)([0-9]{2})([;,:])([0-9]{2})'
        match = re.match(pattern, duration_tc)
        hours, _, minutes, _, seconds, _, _ = match.groups()
        duration_seconds = (
                int(hours) * 60 * 60 +
                int(minutes) * 60 +
                int(seconds)
        )
        return FFmpegInput(md5_hash=md5_hash, file_path=file_path, duration=duration_seconds)


class VideoProbeResult(FFmpegInput):
    """FFmpegInput extended with full stream metadata for VideoDetails."""
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    frame_rate_verbose: str | None = None
    video_codec: str | None = None
    bit_depth: int | None = None
    audio_codec: str | None = None
    audio_bit_depth: int | None = None
    audio_sample_rate: int | None = None
    audio_channels: int | None = None
    duration_tc: str | None = None
    recorded_at: str | None = None
    projection: str | None = None


class ClipPreview(BaseModel):
    md5_hash: str
    frames: int
    frame_height: int
    frame_width: int
    padding: int
    overall_height: int
    overall_width: int
    data: bytes


def _build_frame_command(file_path: str, timestamp: str, width: int, height: int, out_file: str,
                         keyframes_only: bool = True) -> list[str]:
    """Shared ffmpeg command for pulling a single frame at `timestamp` from
    `file_path`. `-threads` (before and after `-i`, covering decode and
    encode) caps ffmpeg's threads instead of the default one-per-core, so
    concurrent jobs don't multiply (#71). `keyframes_only` adds
    `-skip_frame nokey`: the decoder skips everything but keyframes, so
    the output is the first keyframe at/after `timestamp` instead of a frame
    decoded forward from the previous keyframe, which is several times
    cheaper on long-GOP 4K/HEVC footage. Past the last keyframe it yields
    nothing, so callers fall back to `keyframes_only=False`.

    The filter (#80) scales down to fit the `width`x`height` box keeping the
    source aspect ratio (`force_original_aspect_ratio=decrease`,
    `force_divisible_by=2` so an odd scaled dimension never trips ffmpeg),
    then pads the box with black to land on an exact `width`x`height` frame —
    the filmstrip preview (which the frontend and the shot classifier both
    read with a fixed per-frame size) keeps that exact geometry, but
    a non-16:9 source (portrait phone video, 360 dual-fisheye) is pillarboxed/
    letterboxed instead of stretched/distorted. `setsar=1` normalizes the
    output's sample aspect ratio so a non-square-pixel source doesn't still
    look off after the above. ffmpeg autorotates by default (no
    `-noautorotate`), so a rotated portrait phone video is already upright
    before this filter runs, and comes out pillarboxed as expected."""
    threads = str(Environment().get_ffmpeg_threads())
    vf = (
        f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
        f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,'
        f'setsar=1'
    )
    return [
        'ffmpeg', '-nostdin', '-y',
        '-threads', threads,
        *(['-skip_frame', 'nokey'] if keyframes_only else []),
        '-ss', timestamp,
        '-i', file_path,
        '-an', '-sn', '-dn',
        '-vframes', '1',
        '-vf', vf,
        '-threads', threads,
        '-q:v', '2',
        out_file,
    ]


def _extract_frame(file_path: str, timestamp: str, width: int, height: int,
                   out_file: str) -> subprocess.CompletedProcess:
    """Writes one frame to `out_file`: keyframe-only first, accurate decode
    only when that produced nothing (timestamp past the last keyframe)."""
    result = run_niced(_build_frame_command(file_path, timestamp, width, height, out_file),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not Path(out_file).exists():
        result = run_niced(_build_frame_command(file_path, timestamp, width, height, out_file,
                                                keyframes_only=False),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result


def _extract_projection(video_stream: dict) -> str | None:
    """Spherical/equirectangular projection tag from ffprobe's stream
    `side_data_list` (#79) — e.g. {"side_data_type": "Spherical Mapping",
    "projection": "equirectangular"} on a genuine 360 video. Absent on a
    flat video, including an Insta360 camera's own reframed/flat MP4
    exports (scanner/media_type.py deliberately never uses Make alone for
    video, since those exports otherwise look identical)."""
    for side_data in video_stream.get('side_data_list', []) or []:
        projection = side_data.get('projection')
        if projection:
            return str(projection)
    return None


class FFprobe:

    def probe_file(self, md5_hash: str, file_path: str) -> VideoProbeResult | None:
        command = [
            "ffprobe",
            "-i", file_path,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
        ]
        result = run_niced(command, capture_output=True, text=True)
        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        if 'format' not in info:
            return None

        duration = int(float(info['format'].get('duration', 0)))
        tags = info['format'].get('tags', {})
        recorded_at = tags.get('creation_time') or tags.get('com.apple.quicktime.creationdate')

        streams = info.get('streams', [])
        video = next((s for s in streams if s.get('codec_type') == 'video'), None)
        audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)

        probe = VideoProbeResult(
            md5_hash=md5_hash,
            file_path=file_path,
            duration=duration,
            duration_tc=_seconds_to_tc(duration),
            recorded_at=recorded_at,
        )

        if video:
            probe.width = video.get('width')
            probe.height = video.get('height')
            probe.video_codec = video.get('codec_name')
            fr = video.get('r_frame_rate') or video.get('avg_frame_rate')
            if fr:
                probe.frame_rate_verbose = fr
                probe.frame_rate = _eval_frame_rate(fr)
            bps = video.get('bits_per_raw_sample')
            if bps and str(bps) != '0':
                probe.bit_depth = int(bps)
            probe.projection = _extract_projection(video)

        if audio:
            probe.audio_codec = audio.get('codec_name')
            sr = audio.get('sample_rate')
            if sr:
                probe.audio_sample_rate = int(sr)
            probe.audio_channels = audio.get('channels')
            ab = audio.get('bits_per_raw_sample')
            if ab and str(ab) != '0':
                probe.audio_bit_depth = int(ab)

        return probe


class FFmpeg:
    _identifier: str

    def __init__(self, identifier: str):
        self._identifier = identifier

    def _seconds_to_timecode(self, seconds: int) -> str:
        hh = seconds // 3600
        mm = (seconds % 3600) // 60
        ss = seconds % 60
        return f'{hh:02}:{mm:02}:{ss:02}'

    def timestamp_for_keyframes(self, video: FFmpegInput, max_keyframes: int = 5) -> list[str]:
        if video.duration <= 0:
            return [self._seconds_to_timecode(0)]
        step = video.duration / (max_keyframes + 1)
        timestamps = sorted(set(math.floor(step * (i + 1)) for i in range(max_keyframes)))
        return [self._seconds_to_timecode(t) for t in timestamps]

    def generate_clip_preview(
            self,
            video: FFmpegInput,
            width=320,
            height=180,
            padding=10,
            max_keyframes=5
    ) -> ClipPreview:
        timestamps = self.timestamp_for_keyframes(video, max_keyframes=max_keyframes)
        frame_files = []
        # The whole multi-frame extraction is one logical heavy job — the
        # semaphore should reflect "N videos being previewed", not one slot
        # per frame (#71).
        with heavy_slot(f'clip preview {video.file_path}'):
            for i, timestamp in enumerate(timestamps):
                frame_file = f"{self._identifier}_{i}.jpeg"
                result = _extract_frame(video.file_path, timestamp, width, height, frame_file)
                if Path(frame_file).exists():
                    frame_files.append(frame_file)
                else:
                    logging.warning(f'ffmpeg failed to extract frame at {timestamp} from {video.file_path}: {result.stderr.decode(errors="replace")}')

        if not frame_files:
            logging.warning(f'No frames extracted for {video.file_path}, skipping clip preview')
            return None

        images = [Image.open(frame_file) for frame_file in frame_files]
        while len(images) < max_keyframes:
            images.append(images[-1].copy())
        total_width = sum(image.width for image in images) + padding * (len(images) - 1)
        max_height = max(image.height for image in images)
        new_image = Image.new('RGB', (total_width, max_height), (0, 0, 0))

        x_offset = 0
        for image in images:
            new_image.paste(image, (x_offset, 0))
            x_offset += image.width + padding

        buffer = io.BytesIO()
        new_image.save(buffer, format='JPEG')
        image_bytes = buffer.getvalue()

        for file in frame_files:
            Path(file).unlink(missing_ok=True)

        return ClipPreview(
            md5_hash=video.md5_hash,
            frames=len(timestamps),
            frame_height=height,
            frame_width=width,
            padding=padding,
            overall_height=height,
            overall_width=total_width,
            data=image_bytes
        )

    def extract_frames(self, video: FFmpegInput, width=320, height=180, max_keyframes=5) -> list[bytes]:
        """Extract individual frames as a list of JPEG bytes, one per keyframe timestamp."""
        timestamps = self.timestamp_for_keyframes(video, max_keyframes=max_keyframes)
        frames = []
        with heavy_slot(f'frame extraction {video.file_path}'):
            for i, timestamp in enumerate(timestamps):
                frame_file = f"{self._identifier}_frame_{i}.jpeg"
                _extract_frame(video.file_path, timestamp, width, height, frame_file)
                path = Path(frame_file)
                if path.exists():
                    frames.append(path.read_bytes())
                    path.unlink(missing_ok=True)
                else:
                    logging.warning(f'ffmpeg failed to extract frame at {timestamp} from {video.file_path}')
        return frames
