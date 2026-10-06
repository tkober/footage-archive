"""Issue #79 — classify media_type from EXIF/projection metadata instead of
blindly trusting the file extension.

The extension map (`Environment.get_media_type_map()`) stays the source of
truth for which files get scanned, and decides a file's *family*: video vs.
photo, and — for an extension configured under `MEDIA_TYPE_360_VIDEO`/
`MEDIA_TYPE_360_PHOTO` — 360 video vs. 360 photo. Those two env vars are
meant for proprietary, metadata-less 360 formats (`.insv`/`.insp` by
default): there's nothing to check, so an extension mapped there is always
classified as its 360 family, no matter what.

A *plain* video/photo extension (`.mov`/`.mp4`/`.jpg`/`.rw2`/`.dng`/…) is
only promoted to its 360 counterpart when the file's own metadata says so:
  - photo: `Make == 'Arashi Vision'` (Insta360) OR a projection tag naming
    an equirectangular/GPano projection (e.g. an exported 360 JPEG).
  - video: a spherical/equirectangular projection tag. Make alone is NOT
    enough for video — an Insta360 camera's *reframed, flat* MP4 export
    must stay `video`, not `360_video`.

This is what lets a `.dng` from an Insta360 camera be told apart from a
`.dng` written by any other camera (#79), while a probe failure (no
metadata available) simply leaves a file as its plain family type.

`classify_media_type()` is the single place this decision is made — used by
`api/tracking.py::_probe_and_save` to refine the scanner's extension-only
guess once a file has actually been probed."""

_INSTA360_MAKE = 'Arashi Vision'


def classify_media_type(
        extension: str,
        media_type_map: dict[str, str],
        *,
        make: str | None = None,
        projection: str | None = None,
) -> str | None:
    """Classify a file's final `media_type`.

    `extension` is looked up (case-insensitively) in `media_type_map` (as
    produced by `Environment.get_media_type_map()`) for the base family.
    Returns None for an extension not present in the map (unrecognised /
    not scanned).
    """
    base = media_type_map.get(extension.lower())
    if base is None:
        return None

    # Extensions explicitly configured as "360 only" formats (.insp/.insv by
    # default) carry no usable metadata to check — always their 360 family.
    if base in ('360_video', '360_photo'):
        return base

    if base == 'photo':
        if make == _INSTA360_MAKE or _is_equirectangular(projection):
            return '360_photo'
        return 'photo'

    if base == 'video':
        if _is_equirectangular(projection):
            return '360_video'
        return 'video'

    return base


def _is_equirectangular(projection: str | None) -> bool:
    if not projection:
        return False
    return 'equirectangular' in projection.strip().lower()
