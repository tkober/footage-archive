import io
import logging
from fractions import Fraction
from pathlib import Path

import numpy as np
import rawpy
from PIL import Image, ImageOps
from PIL.ExifTags import TAGS
from pydantic import BaseModel


class PhotoProbeResult(BaseModel):
    md5_hash: str
    file_path: str
    width: int | None = None
    height: int | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    iso: int | None = None
    aperture: float | None = None
    shutter_speed: str | None = None
    focal_length: float | None = None
    color_space: str | None = None
    bit_depth: int | None = None
    recorded_at: str | None = None
    latitude: float | None = None
    longitude: float | None = None


def probe_photo(md5_hash: str, file_path: str) -> PhotoProbeResult | None:
    try:
        img = Image.open(file_path)
        width, height = img.size

        result = PhotoProbeResult(
            md5_hash=md5_hash,
            file_path=file_path,
            width=width,
            height=height,
        )

        raw_exif = img._getexif() if hasattr(img, '_getexif') else None
        if not raw_exif:
            return result

        exif = {TAGS.get(k, k): v for k, v in raw_exif.items()}

        result.camera_make = _str(exif.get('Make'))
        result.camera_model = _str(exif.get('Model'))
        result.iso = _int(exif.get('ISOSpeedRatings') or exif.get('PhotographicSensitivity'))
        result.recorded_at = _str(exif.get('DateTimeOriginal') or exif.get('DateTime'))

        f_number = exif.get('FNumber')
        if f_number is not None:
            result.aperture = round(float(Fraction(f_number)), 1)

        exp_time = exif.get('ExposureTime')
        if exp_time is not None:
            raw = Fraction(exp_time)
            f = Fraction(raw.numerator, raw.denominator).limit_denominator(10000)
            result.shutter_speed = f'1/{f.denominator}' if f.numerator == 1 else str(f)

        focal = exif.get('FocalLength')
        if focal is not None:
            result.focal_length = round(float(Fraction(focal)), 1)

        cs = exif.get('ColorSpace')
        if cs == 1:
            result.color_space = 'sRGB'
        elif cs == 65535:
            result.color_space = 'Uncalibrated'
        elif cs is not None:
            result.color_space = str(cs)

        bps = exif.get('BitsPerSample')
        if bps is not None:
            result.bit_depth = _int(bps[0] if isinstance(bps, tuple) else bps)

        gps = exif.get('GPSInfo')
        if gps and isinstance(gps, dict):
            result.latitude = _parse_gps_dms(gps.get(2), gps.get(1))
            result.longitude = _parse_gps_dms(gps.get(4), gps.get(3))

        return result

    except Exception as e:
        logging.debug(f'Photo probe failed for {file_path}: {e}')
        return None


def generate_photo_thumbnail(md5_hash: str, file_path: str, max_width: int = 600) -> bytes | None:
    try:
        ext = Path(file_path).suffix.lower()
        if ext == '.rw2':
            img = _open_rw2(file_path)
        else:
            img = Image.open(file_path)
            img = ImageOps.exif_transpose(img)
        if img is None:
            return None
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=82)
        return buf.getvalue()
    except Exception as e:
        logging.debug(f'Photo thumbnail generation failed for {file_path}: {e}')
        return None


def _open_rw2(file_path: str) -> Image.Image | None:
    with rawpy.imread(file_path) as raw:
        rgb = raw.postprocess(
            use_camera_wb=True,
            half_size=False,
            no_auto_bright=False,
            output_bps=8,
        )
    return Image.fromarray(np.asarray(rgb, dtype=np.uint8))


def _parse_gps_dms(dms, ref) -> float | None:
    """Convert GPS degrees/minutes/seconds tuple + N/S/E/W ref to decimal degrees."""
    if not dms or not ref:
        return None
    try:
        degrees = float(Fraction(dms[0]))
        minutes = float(Fraction(dms[1]))
        seconds = float(Fraction(dms[2]))
        decimal = degrees + minutes / 60 + seconds / 3600
        if ref in ('S', 'W'):
            decimal = -decimal
        return round(decimal, 6)
    except Exception:
        return None


def _str(val) -> str | None:
    return str(val).strip() if val is not None else None


def _int(val) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
