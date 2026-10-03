"""Process-wide reader/writer path lock registry.

Two paths "overlap" if they are equal, or one is an ancestor of the other
(checked with ``Path.is_relative_to`` on *resolved* paths — never string
``startswith``, which would wrongly treat ``/a/b`` as an ancestor of
``/a/bc``).

- ``shared(path)`` — used by scans/tracking/import tasks. Blocks (waits)
  while an overlapping *exclusive* lock is held by someone else.
- ``try_exclusive(paths)`` — used by move/rename. Raises :class:`PathLockedError`
  immediately (no waiting) if any overlapping shared or exclusive lock is
  already held.

There is only ever one backend process, so a simple in-memory registry
guarded by a single ``threading.Lock`` is enough; no cross-process locking
is needed.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable


class PathLockedError(Exception):
    """Raised by try_exclusive() when an overlapping lock is already held."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f'Path is locked by a running operation: {path}')


def _resolve(path: str) -> Path:
    return Path(path).resolve()


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


class _PathLockRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        # Resolved path -> count of active shared holders.
        self._shared: dict[Path, int] = {}
        # Resolved paths currently held exclusively.
        self._exclusive: set[Path] = set()

    def _any_exclusive_overlap(self, path: Path) -> bool:
        return any(_overlaps(path, ex) for ex in self._exclusive)

    def _any_lock_overlap(self, path: Path) -> bool:
        if self._any_exclusive_overlap(path):
            return True
        return any(_overlaps(path, shared) for shared in self._shared)

    def acquire_shared(self, path: Path) -> None:
        with self._condition:
            while self._any_exclusive_overlap(path):
                self._condition.wait()
            self._shared[path] = self._shared.get(path, 0) + 1

    def release_shared(self, path: Path) -> None:
        with self._condition:
            count = self._shared.get(path, 0) - 1
            if count <= 0:
                self._shared.pop(path, None)
            else:
                self._shared[path] = count
            self._condition.notify_all()

    def try_acquire_exclusive(self, paths: list[Path]) -> None:
        with self._condition:
            for path in paths:
                if self._any_lock_overlap(path):
                    raise PathLockedError(str(path))
            for path in paths:
                self._exclusive.add(path)

    def release_exclusive(self, paths: list[Path]) -> None:
        with self._condition:
            for path in paths:
                self._exclusive.discard(path)
            self._condition.notify_all()


_registry = _PathLockRegistry()


@contextmanager
def shared(path: str):
    """Hold a shared (reader) lock on ``path`` for the duration of the block.

    Blocks while an overlapping exclusive lock is held elsewhere.
    """
    resolved = _resolve(path)
    _registry.acquire_shared(resolved)
    try:
        yield
    finally:
        _registry.release_shared(resolved)


@contextmanager
def try_exclusive(paths: Iterable[str]):
    """Try to acquire exclusive (writer) locks on all ``paths`` at once.

    Raises :class:`PathLockedError` immediately if any overlapping shared or
    exclusive lock is already held — never waits.
    """
    resolved = [_resolve(p) for p in paths]
    _registry.try_acquire_exclusive(resolved)
    try:
        yield
    finally:
        _registry.release_exclusive(resolved)
