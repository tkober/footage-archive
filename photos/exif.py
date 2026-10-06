import io
import json
import logging
from pathlib import Path

import numpy as np
import rawpy
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel

from tasks.loadcontrol import heavy_slot, run_niced

# Every extension here is sent through rawpy for thumbnailing/full-res rendering
# (#78) — never through Pillow, which can't open most of these (incl. Insta360
# .dng). An extension in this set is therefore never recorded 'unsupported';
# any rawpy/libraw failure on it is 'failed' instead (see generate_photo_thumbnail).
RAW_EXTENSIONS = {'.rw2', '.dng', '.cr2', '.cr3', '.nef', '.arw', '.orf', '.raf'}

# Insta360 X3 (and presumably other Insta360 models) write their DNG's Make tag
# as this value. Those DNGs decode with a strong magenta cast under camera WB
# and come out of libraw portrait instead of the camera's own landscape 2:1
# (.insp) framing — both corrected in _postprocess_raw (#78).
_INSTA360_MAKE = 'Arashi Vision'


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
        result = run_niced(
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
        result = run_niced(
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


def generate_photo_thumbnail(
        md5_hash: str, file_path: str, max_width: int = 600,
) -> tuple[bytes | None, str, str | None]:
    """600px-wide JPEG thumbnail (Pillow, EXIF-rotation-corrected; rawpy for
    any `RAW_EXTENSIONS` format, #78) plus *why* it failed (#77), so the
    caller can record a `PreviewStatus` outcome. PIL's `UnidentifiedImageError`
    on an extension Pillow doesn't handle is reported as ``'unsupported'``;
    a RAW extension is never ``'unsupported'`` — any rawpy/libraw failure on
    one is ``'failed'``, same as any other failure (incl. a corrupt file with
    an extension Pillow does handle). Returns ``(thumbnail_bytes, 'ok', None)``
    on success."""
    ext = Path(file_path).suffix.lower()
    try:
        with heavy_slot(f'photo thumbnail {file_path}'):
            if ext in RAW_EXTENSIONS:
                img = _raw_thumbnail(file_path, max_width)
            else:
                img = Image.open(file_path)
                img = ImageOps.exif_transpose(img)
            if img is None:
                return None, 'failed', 'No image data produced'
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=82)
            return buf.getvalue(), 'ok', None
    except UnidentifiedImageError as e:
        logging.warning(f'Photo thumbnail generation failed for {file_path}: {e}')
        if ext in Image.registered_extensions():
            return None, 'failed', str(e)
        return None, 'unsupported', f'Format not supported by the preview generator ({ext})'
    except Exception as e:
        logging.warning(f'Photo thumbnail generation failed for {file_path}: {e}')
        return None, 'failed', str(e)


def _raw_thumbnail(file_path: str, max_width: int) -> Image.Image | None:
    """Prefer the camera's embedded preview (fast) when it's at least as wide as
    `max_width`; otherwise fall back to a half-size rawpy postprocess (#78)."""
    img = _extract_raw_preview(file_path)
    if img is not None and img.width >= max_width:
        return img
    return _postprocess_raw(file_path, half_size=True)


def render_full_raw(file_path: str) -> bytes | None:
    """Native-resolution JPEG from a RAW file (any `RAW_EXTENSIONS` format, #78).

    Prefers the camera's embedded preview when the RAW carries one (fast —
    e.g. the ~500KB JPEG Lumix RW2 embeds); otherwise falls back to the full
    libraw postprocess to get the original sensor resolution (slower, but
    this backs the detailed comparison view where resolution is the whole
    point). Insta360 .dng carries no embedded preview, so it always takes the
    postprocess path.
    """
    try:
        with heavy_slot(f'full raw render {file_path}'):
            img = _extract_raw_preview(file_path)
            if img is None:
                img = _postprocess_raw(file_path, half_size=False)
            if img is None:
                return None
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=92)
            return buf.getvalue()
    except Exception as e:
        logging.warning(f'Full RAW render failed for {file_path}: {e}')
        return None


def _extract_raw_preview(file_path: str) -> Image.Image | None:
    """The camera's own embedded preview via rawpy's `extract_thumb()`
    (JPEG, EXIF-rotation-corrected, or a raw bitmap) — None if the RAW file
    carries no usable embedded preview (e.g. an Insta360 .dng, which has
    none at all)."""
    try:
        with rawpy.imread(file_path) as raw:
            thumb = raw.extract_thumb()
    except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
        return None
    except Exception as e:
        logging.debug(f'RAW embedded-thumb extraction failed for {file_path}: {e}')
        return None
    if thumb.format == rawpy.ThumbFormat.JPEG:
        img = Image.open(io.BytesIO(thumb.data))
        return ImageOps.exif_transpose(img)
    if thumb.format == rawpy.ThumbFormat.BITMAP:
        return Image.fromarray(thumb.data)
    return None


def _postprocess_raw(file_path: str, half_size: bool = False) -> Image.Image | None:
    """Full libraw demosaic (works for any libraw RAW format, not just RW2 —
    renamed from `_open_rw2` in #78). Insta360 (Make == 'Arashi Vision', #78):
    `use_auto_wb` instead of `use_camera_wb` (camera WB renders a strong
    magenta cast on these), and — since libraw decodes the DNG portrait while
    the camera's own .insp preview is landscape 2:1 — a 90° counterclockwise
    rotation of a portrait result, the direction confirmed empirically by
    rendering both ways and comparing against the sibling .insp (see #78 PR).
    Every other camera keeps the original `use_camera_wb=True`, no rotation."""
    is_insta360 = _camera_make(file_path) == _INSTA360_MAKE
    postprocess_kwargs = dict(half_size=half_size, no_auto_bright=False, output_bps=8)
    if is_insta360:
        postprocess_kwargs['use_auto_wb'] = True
    else:
        postprocess_kwargs['use_camera_wb'] = True
    with rawpy.imread(file_path) as raw:
        rgb = raw.postprocess(**postprocess_kwargs)
    img = Image.fromarray(np.asarray(rgb, dtype=np.uint8))
    if is_insta360 and img.height > img.width:
        img = img.rotate(90, expand=True)
    return img


def _camera_make(file_path: str) -> str | None:
    """Single cheap exiftool tag read, used only on the RAW postprocess path
    (#78) to detect an Insta360 DNG needing its own WB/rotation correction."""
    try:
        result = run_niced(
            ['exiftool', '-json', '-Make', file_path],
            capture_output=True, text=True,
        )
        data = json.loads(result.stdout)[0]
        return _str(data.get('Make'))
    except Exception as e:
        logging.debug(f'exiftool Make lookup failed for {file_path}: {e}')
        return None


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
