"""Stat-only directory census (#138, epic #133) — walks a tree and reports,
per directory, how many of its DIRECT files are "relevant" (would be
scanned), without ever hashing or even `os.stat()`-ing a file. Used by
`api/tracking.py`'s scan planner to show the whole per-directory unit
breakdown *before* any work starts; `tasks/directory_stats.py`'s census
(#139) reuses the same per-directory scan (`scan_directory_entries`) plus
`walk_directories_census` below, which also reports each directory's parent
and the subdirectories actually walked — everything the census needs to
compute DirectoryStats bottom-up in memory, in one pass, with no extra
per-directory DB query."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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


@dataclass
class DirectoryCensusEntry:
    """One directory from `walk_directories_census` (#139): like
    `DirectoryCount`, plus its parent (`None` for `root` itself) and the
    *direct* subdirectories actually walked below it (same skip rules as
    the file count — hidden/junk, symlinked, and the trash directory itself
    are never included here either, so a census's bottom-up pass never
    treats the trash as an "unknown" child it can't account for)."""
    directory: str
    parent: Optional[str]
    media_file_count: int
    subdirectories: list[str] = field(default_factory=list)


def scan_directory_entries(directory: Path) -> Optional[tuple[int, list[Path]]]:
    """One `os.scandir()` pass over `directory`: how many of its DIRECT
    files are "relevant" (#138's rule — lowercase extension in
    `Environment.get_scanning_file_extensions()`, not hidden) plus the real
    subdirectories below it (hidden/junk directories, symlinks to a file or
    a directory, and the trash directory itself are never included — the
    trash check matters for `walk_directories_census`: without it, the
    trash dir would show up as a "subdirectory" with no `DirectoryCensusEntry`
    of its own, which would make its parent look incomplete forever).
    `DirEntry.is_file()`/`is_dir()` with `follow_symlinks=False` are enough
    for both, and both already came free from the single `readdir()` call
    `os.scandir` made — no hashing, no per-file `os.stat()`.

    Returns `None` if the directory can't be read (permissions, a race with
    a delete) — the caller decides what to log and how to react; this
    function is shared by #138's planner walk and #139's census/refresh, and
    each treats an unreadable directory slightly differently."""
    env = Environment()
    scanning_extensions = set(env.get_scanning_file_extensions())
    hidden_extensions = set(env.get_browser_hidden_extensions())
    hidden_names = set(env.get_browser_hidden_names())
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return None

    media_file_count = 0
    subdirs: list[Path] = []
    for entry in entries:
        if is_hidden_system_file(entry.name, hidden_extensions, hidden_names):
            continue
        if entry.is_file(follow_symlinks=False):
            if os.path.splitext(entry.name)[1].lower() in scanning_extensions:
                media_file_count += 1
        elif entry.is_dir(follow_symlinks=False):
            sub_path = Path(entry.path)
            if is_in_trash(sub_path):
                continue
            subdirs.append(sub_path)
        # Anything else (a symlink, since follow_symlinks=False makes a
        # symlinked file/dir neither is_file() nor is_dir() here) is simply
        # never counted and never descended into.
    return media_file_count, subdirs


def walk_directories(root: Path | str) -> list[DirectoryCount]:
    """Recursively walks `root` (included) via `scan_directory_entries`,
    never hashing or `os.stat()`-ing a file.

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
    results: list[DirectoryCount] = []

    def visit(directory: Path) -> None:
        if is_in_trash(directory):
            return
        scanned = scan_directory_entries(directory)
        if scanned is None:
            logger.warning('Could not read directory "%s" while planning a scan', directory, exc_info=True)
            return
        media_file_count, subdirs = scanned
        results.append(DirectoryCount(directory=str(directory), media_file_count=media_file_count))
        for sub in subdirs:
            visit(sub)

    visit(Path(root))
    return results


def walk_directories_census(root: Path | str) -> list[DirectoryCensusEntry]:
    """Like `walk_directories`, but for #139's census: also reports each
    directory's parent and the direct subdirectories actually walked below
    it, so `tasks/directory_stats.py::run_census` can compute every
    directory's `subtree_*`/`subtree_complete` bottom-up, in memory, from
    this one walk plus one tracked-count query — no second filesystem pass,
    no per-directory DB round-trip. A subdirectory that couldn't be read
    (logged here, not fatal) is simply absent from the result, which is
    exactly what makes its parent `subtree_complete=False`, not
    "0 relevant files below" (`tasks/directory_stats.py`'s aggregation
    treats a missing child the same way, see its docstring)."""
    results: list[DirectoryCensusEntry] = []
    root_path = Path(root)

    def visit(directory: Path, parent: Optional[Path]) -> None:
        if is_in_trash(directory):
            return
        scanned = scan_directory_entries(directory)
        if scanned is None:
            logger.warning('Could not read directory "%s" during a census walk', directory, exc_info=True)
            return
        media_file_count, subdirs = scanned
        results.append(DirectoryCensusEntry(
            directory=str(directory),
            parent=str(parent) if parent is not None else None,
            media_file_count=media_file_count,
            subdirectories=[str(s) for s in subdirs],
        ))
        for sub in subdirs:
            visit(sub, directory)

    visit(root_path, None)
    return results
