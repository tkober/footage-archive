"""Process-wide registry of md5 hashes whose preview is currently queued or
being generated (#77) — same single-process, in-memory reasoning as
``fileops/pathlocks.py`` (there's only ever one backend process, so a plain
``threading.Lock``-guarded set is enough, no cross-process locking needed).

Backs the derived ``preview_status``'s ``"generating"`` value: a file with
no ``ClipPreviews`` row and no ``PreviewStatus`` row, but whose hash is in
this registry, is being worked on right now rather than simply never
attempted.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterable

_lock = threading.Lock()
_pending: set[str] = set()


@contextmanager
def pending_previews(hashes: Iterable[str]):
    """Register ``hashes`` as pending for the duration of the block, so
    ``is_pending`` reports them as "generating" while a batch task (scan,
    rediscover, rescan, missing-preview repair) works through them."""
    hashes = list(hashes)
    with _lock:
        _pending.update(hashes)
    try:
        yield
    finally:
        with _lock:
            _pending.difference_update(hashes)


def discard(md5_hash: str) -> None:
    """Remove a single hash once its preview attempt is done. Called from
    ``generate_preview``'s ``finally`` so a hash drops out of "generating"
    the moment its own attempt finishes, even before the whole batch (which
    may cover many hashes) completes. Safe to call even if the hash was
    never registered."""
    with _lock:
        _pending.discard(md5_hash)


def is_pending(md5_hash: str) -> bool:
    with _lock:
        return md5_hash in _pending
