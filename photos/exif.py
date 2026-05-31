import io
import json
import logging
import subprocess
from pathlib import Path

import numpy as np
import rawpy
from PIL import Image, ImageOps
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
    lens: str | None = None
    focal_length_35mm: float | None = None
    scale_factor_35mm: float | None = None
    field_of_view: float | None = None
    recorded_at: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None


# exiftool tags requested for every photo (JPEG, RW2, …). A trailing '#' forces the
# raw numeric value (e.g. FocalLength 5.4 instead of "5.4 mm"). Tags without '#' keep
# exiftool's human-readable form, which is what we want for ColorSpace ("sRGB"),
# ExposureTime ("1/50") and the friendly LensType name. Note: the "Field Of View"
# composite is named 'FOV' in exiftool's JSON output, not 'FieldOfView'.
_EXIFTOOL_TAGS = [
    '-Make', '-Model', '-ISO#', '-FNumber#', '-ExposureTime', '-FocalLength#',
    '-ColorSpace', '-BitsPerSample', '-DateTimeOriginal', '-ImageWidth', '-ImageHeight',
    '-LensType', '-LensID', '-LensModel',
    '-FocalLengthIn35mmFormat#', '-ScaleFactor35efl#', '-FOV#',
    '-GPSLatitude#', '-GPSLatitudeRef#', '-GPSLongitude#', '-GPSLongitudeRef#',
    '-GPSAltitude#', '-GPSAltitudeRef#',
]


def dump_all_exif(file_path: str) -> list[dict]:
    """Return every tag exiftool can read for a file as an ordered list of
    {group, tag, value} dicts. Used by the read-on-demand 'all metadata' endpoint.

    Uses -G1 so each key is prefixed with its group (e.g. 'EXIF:Make', 'GPS:GPSLatitude',
    'Composite:FOV'), keeping same-named tags from different groups distinct. Binary tags
    come back as a human placeholder string ('(Binary data N bytes, ...)'), which we keep.
    """
    try:
        result = subprocess.run(
            ['exiftool', '-json', '-G1', file_path],
            capture_output=True, text=True,
        )
        data = json.loads(result.stdout)[0]
    except Exception as e:
        logging.debug(f'exiftool full dump failed for {file_path}: {e}')
        return []

    tags = []
    for key, val in data.items():
        if key == 'SourceFile':  # absolute server path, redundant with the requested file
            continue
        group, sep, tag = key.partition(':')
        if not sep:  # ungrouped key has no 'Group:' prefix
            group, tag = '', group
        tags.append({'group': group, 'tag': tag, 'value': _stringify(val)})
    return tags


def probe_photo(md5_hash: str, file_path: str) -> PhotoProbeResult | None:
    """Extract photo metadata via exiftool. Used for all photo formats (JPEG, RW2, …)."""
    try:
        result = subprocess.run(
            ['exiftool', '-json', *_EXIFTOOL_TAGS, file_path],
            capture_output=True, text=True,
        )
        data = json.loads(result.stdout)[0]
    except Exception as e:
        logging.debug(f'exiftool probe failed for {file_path}: {e}')
        return None

    probe = PhotoProbeResult(md5_hash=md5_hash, file_path=file_path)
    probe.width = _int(data.get('ImageWidth'))
    probe.height = _int(data.get('ImageHeight'))
    probe.camera_make = _str(data.get('Make'))
    probe.camera_model = _str(data.get('Model'))
    probe.iso = _int(data.get('ISO'))
    probe.recorded_at = _str(data.get('DateTimeOriginal'))
    probe.color_space = _str(data.get('ColorSpace'))
    probe.bit_depth = _int(data.get('BitsPerSample'))
    probe.lens = _str(data.get('LensType') or data.get('LensID') or data.get('LensModel'))

    probe.aperture = _round(data.get('FNumber'), 1)
    # exiftool returns ExposureTime already formatted as "1/6400"
    probe.shutter_speed = _str(data.get('ExposureTime'))
    probe.focal_length = _round(data.get('FocalLength'), 1)
    probe.focal_length_35mm = _round(data.get('FocalLengthIn35mmFormat'), 1)
    probe.scale_factor_35mm = _round(data.get('ScaleFactor35efl'), 2)
    probe.field_of_view = _round(data.get('FOV'), 1)

    # GPS: exiftool returns unsigned decimal degrees + a separate N/S/E/W ref
    lat = data.get('GPSLatitude')
    lat_ref = data.get('GPSLatitudeRef')
    lon = data.get('GPSLongitude')
    lon_ref = data.get('GPSLongitudeRef')
    if lat is not None and lat_ref is not None:
        probe.latitude = round(float(lat) * (-1 if lat_ref == 'S' else 1), 6)
    if lon is not None and lon_ref is not None:
        probe.longitude = round(float(lon) * (-1 if lon_ref == 'W' else 1), 6)

    # GPSAltitude is unsigned metres; GPSAltitudeRef 0 = above sea level, 1 = below
    alt = data.get('GPSAltitude')
    if alt is not None:
        probe.altitude = round(float(alt) * (-1 if data.get('GPSAltitudeRef') == 1 else 1), 1)

    return probe


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


def _str(val) -> str | None:
    return str(val).strip() if val is not None else None


def _stringify(val) -> str:
    """Flatten any exiftool JSON value (number, list, XMP struct) to a display string."""
    if isinstance(val, list):
        return ', '.join(_stringify(v) for v in val)
    return str(val)


def _int(val) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _round(val, ndigits: int) -> float | None:
    try:
        return round(float(val), ndigits)
    except (TypeError, ValueError):
        return None
