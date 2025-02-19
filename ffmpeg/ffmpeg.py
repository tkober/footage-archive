import math
import re
import io
import subprocess
import json
from PIL import Image
from pydantic import BaseModel


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


class ClipPreview(BaseModel):
    md5_hash: str
    frames: int
    frame_height: int
    frame_width: int
    padding: int
    overall_height: int
    overall_width: int
    data: bytes


class FFprobe:

    def probe_file(self, md5_hash: str, file_path: str) -> FFmpegInput | None:
        command = [
            "ffprobe",
            "-i", file_path,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format"
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        info = json.loads(result.stdout)
        if 'format' not in info:
            return None

        duration = int(float(info['format']['duration']))
        return FFmpegInput(md5_hash=md5_hash, file_path=file_path, duration=duration)


class FFmpeg:
    _identifier: str

    def __init__(self, identifier: str):
        self._identifier = identifier

    def _seconds_to_timecode(self, seconds: int) -> str:
        hh = seconds // 3600
        mm = (seconds % 3600) // 60
        ss = seconds % 60
        return f'{hh:02}:{mm:02}:{ss:02}'

    def timestamp_for_keyframes(self, video: FFmpegInput, padding: int = 1, max_keyframes: int = 5) -> [str]:

        if video.duration >= padding * 3:
            steps = (video.duration - padding) / max_keyframes
            result = [
                math.floor((i + 1) * steps)
                for i in range(max_keyframes)
            ]
            result = [
                i for i in result
                if padding <= i <= (video.duration - padding)
            ]
            result = [self._seconds_to_timecode(i) for i in set(result)]
            result.sort()
            return result

        if video.duration >= padding * 2:
            return [str(self._seconds_to_timecode(padding))]

        return [str(self._seconds_to_timecode(0))]

    def generate_clip_preview(
            self,
            video: FFmpegInput,
            width=320,
            height=180,
            padding=10
    ) -> ClipPreview:
        frame_files = []
        timestamps = self.timestamp_for_keyframes(video)
        for i, timestamp in enumerate(timestamps):
            frame_file = f"{self._identifier}_{i}.jpeg"
            command = [
                'ffmpeg',
                '-ss', timestamp,
                '-i', video.file_path,
                '-vframes', '1',
                '-vf', f'scale={width}:{height}',
                '-q:v', '2',
                frame_file
            ]
            subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            frame_files.append(frame_file)

        images = [Image.open(frame_file) for frame_file in frame_files]
        total_width = sum(image.width for image in images) + padding * (len(images) - 1)
        max_height = max(image.height for image in images)
        new_image = Image.new('RGB', (total_width, max_height), (0, 0, 0))  # Schwarz als Hintergrund

        x_offset = 0
        for image in images:
            new_image.paste(image, (x_offset, 0))
            x_offset += image.width + padding

        buffer = io.BytesIO()
        new_image.save(buffer, format='JPEG')
        image_bytes = buffer.getvalue()

        for file in frame_files:
            subprocess.run(['rm', file])

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
