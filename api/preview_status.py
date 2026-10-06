"""Derived ``preview_status`` (#77) — a single status string for a tracked,
previewable file (``media_type`` in ``VIDEO_TYPES | PHOTO_TYPES``), surfaced
by the directory listing, file-details, search and list-item APIs so the
frontend can render the right media-card tile without a 404 round-trip.

Precedence:
1. A ``ClipPreviews`` row exists -> ``"ok"``.
2. Else the hash is in the "pending previews" registry (a scan/rescan/
   repair task has it queued or in flight right now) -> ``"generating"``.
3. Else a ``PreviewStatus`` row with status ``failed``/``unsupported`` ->
   that value.
4. Else -> ``"missing"`` (never attempted).

Untracked or non-media files -> ``None``.
"""

from typing import Optional

from api.tracking import PHOTO_TYPES, VIDEO_TYPES
from tasks.preview_registry import is_pending

PREVIEWABLE_MEDIA_TYPES = VIDEO_TYPES | PHOTO_TYPES


def derive_preview_status(md5_hash: Optional[str], media_type: Optional[str],
                           has_preview: bool, failed_status: Optional[str] = None) -> Optional[str]:
    if media_type not in PREVIEWABLE_MEDIA_TYPES:
        return None
    if has_preview:
        return 'ok'
    if md5_hash and is_pending(md5_hash):
        return 'generating'
    if failed_status in ('failed', 'unsupported'):
        return failed_status
    return 'missing'
