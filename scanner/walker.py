"""Stat-only directory census (#138, epic #133) — walks a tree and reports,
per directory, how many of its DIRECT files are "relevant" (would be
scanned), without ever hashing or even `os.stat()`-ing a file. Used by
`api/tracking.py`'s scan planner to show the whole per-directory unit
breakdown *before* any work starts; a later ticket's census walk reuses it
too, hence its own module rather than living in `api/tracking.py`."""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from env.environment import Environment
from env.hidden_files import is_hidden_system_file
from fileops.trash import is_in_trash

logger = logging.getLogger(__name__)


@dataclass
class DirectoryCount:
    """One directory from the walk: its path (built the same way the
    scanner builds `ScanResult.directory` — `str(path.parent)` of a
    candidate — so it lines up with `Files.directory`/`ScanUnits.directory`)
    plus how many of its *direct* files are relevant. A directory with 0
    relevant files is still reported (e.g. `Nara` in the ticket's example,
    which only has relevant files in its own subdirectories) — the caller
    decides what counts as a unit."""
    directory: str
    media_file_count: int


def walk_directories(root: Path | str) -> list[DirectoryCount]:
    """Recursively walks `root` (included) via `os.scandir`, never hashing
    or `os.stat()`-ing a file — `DirEntry.is_file(follow_symlinks=False)`/
    `is_dir(follow_symlinks=False)` are enough, and both already come free
    from the single `readdir()` call `os.scandir` made (a symlink, to a
    file or a directory, is neither when not following it, so it's simply
    never counted and never descended into).

    Skipped, same rules a normal scan already applies (scanner/scanner.py,
    fileops/trash.py): the trash directory itself (and anything under it —
    never descended into), and any hidden/junk file or directory (AppleDouble
    `._*`, `BROWSER_HIDDEN_NAMES`/`BROWSER_HIDDEN_EXTENSIONS`, via
    `env/hidden_files.py::is_hidden_system_file` — this is also why a `._*`
    AppleDouble sidecar of a relevant file, which shares its extension, is
    never miscounted as relevant itself). "Relevant" = lowercase extension
    in `Environment.get_scanning_file_extensions()` — a sidecar extension
    like `.xmp` or an unrelated one like `.txt` never counts.

    An unreadable subdirectory (permissions, a race with a delete) is
    logged and skipped, not fatal to the rest of the walk."""
    env = Environment()
    scanning_extensions = set(env.get_scanning_file_extensions())
    hidden_extensions = set(env.get_browser_hidden_extensions())
    hidden_names = set(env.get_browser_hidden_names())

    results: list[DirectoryCount] = []

    def visit(directory: Path) -> None:
        if is_in_trash(directory):
            return
        try:
            entries = list(os.scandir(directory))
        except OSError:
            logger.warning('Could not read directory "%s" while planning a scan', directory, exc_info=True)
            return

        media_file_count = 0
        subdirs: list[Path] = []
        for entry in entries:
            if is_hidden_system_file(entry.name, hidden_extensions, hidden_names):
                continue
            if entry.is_file(follow_symlinks=False):
                if os.path.splitext(entry.name)[1].lower() in scanning_extensions:
                    media_file_count += 1
            elif entry.is_dir(follow_symlinks=False):
                subdirs.append(Path(entry.path))
            # Anything else (a symlink, since follow_symlinks=False makes a
            # symlinked file/dir neither is_file() nor is_dir() here) is
            # simply never counted and never descended into.

        results.append(DirectoryCount(directory=str(directory), media_file_count=media_file_count))
        for sub in subdirs:
            visit(sub)

    visit(Path(root))
    return results
