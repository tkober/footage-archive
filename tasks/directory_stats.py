"""Recursive directory status (#139, epic #133) — `DirectoryStats` rows so
the browser's folder tiles can show whether there are untracked files
*below* a folder without the browser's own request ever computing anything
recursive (`api/files.py::query_directory` just reads the row). A row is
written by whatever already touches a directory: a finished scan unit
(`tasks/scan_hooks.py`), a fileops rename/move/trash/mkdir
(`fileops/service.py`), a `Track file`/`Rediscover` task (`api/tracking.py`),
or an explicit "Scan untracked only"-adjacent census walk (`run_census`
below, `POST /tracking/census`).

Two primitives:
- `refresh_directory(directory, source)` — one directory's own counts
  (`scanner/walker.py::scan_directory_entries`, the same per-directory scan
  #138's planner uses) plus its subtree totals, rolled up from its
  children's ALREADY-STORED rows (one query, `WHERE parent = :dir` — never
  recursive itself).
- `refresh_chain(directory, source)` — `refresh_directory` for `directory`,
  then for every ancestor up to and including `ROOT_DIR` (never above it),
  so a leaf's own change is reflected all the way up without a single
  directory being rescanned recursively.

`run_census(path, report)` is the one case that *does* walk a whole subtree
at once (`scanner/walker.py::walk_directories_census`) — e.g. after a user
asks to "Refresh status" for a folder that's never been touched, where
there's no existing child row to roll up from at all.
"""

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from db.database import Database
from env.environment import Environment
from fileops.trash import is_in_trash
from scanner.walker import scan_directory_entries, walk_directories_census

logger = logging.getLogger(__name__)

# Serializes every DirectoryStats write: refresh_directory's own
# read-children-then-upsert, and the census's bulk upsert+cleanup, all go
# through this one process-wide lock. Without it, two consumer threads
# finishing sibling scan units at (almost) the same moment could both read
# their shared parent's current row, add their own child's contribution to
# it, and write back — a classic read-modify-write race where the second
# write silently loses the first, leaving the parent's subtree totals wrong
# until something else happens to refresh it again. Each critical section
# (one directory's read+compute+write, or one census's whole bulk write) is
# short enough that a single process-wide lock is simpler than per-row DB
# locking, and there's only ever one backend process (same reasoning as
# fileops/pathlocks.py).
_lock = threading.Lock()

# In-memory dedupe for POST /tracking/census (#139): a path that's already
# QUEUED/RUNNING as a Census task is rejected (409) rather than started a
# second time — shared between the manual endpoint and the automatic
# post-scan-job trigger (tasks/scanqueue.py), so the two never race each
# other either. Same "only one backend process" assumption as the lock
# above; a restart simply loses an in-flight census, which is harmless
# (nothing relies on it having finished).
_census_lock = threading.Lock()
_active_census_paths: set[str] = set()


def try_start_census(path: str) -> bool:
    """Reserves `path` for a census if none is already active for it.
    Returns False (reserves nothing) if one is."""
    with _census_lock:
        if path in _active_census_paths:
            return False
        _active_census_paths.add(path)
        return True


def finish_census(path: str) -> None:
    with _census_lock:
        _active_census_paths.discard(path)


def refresh_directory(directory: str, source: str) -> None:
    """Recomputes `directory`'s own DirectoryStats row from the filesystem
    (own counts) plus its children's already-stored rows (subtree totals) —
    never recurses into a child itself, so this is always a handful of
    scandirs/queries regardless of how big the tree below `directory` is.
    A directory that no longer exists on disk (or is the trash, or inside
    it) has its own row and every row below it deleted instead."""
    with _lock:
        _refresh_directory_locked(directory, source)


def _refresh_directory_locked(directory: str, source: str) -> None:
    db = Database()
    path = Path(directory)
    if is_in_trash(path) or not path.is_dir():
        db.delete_directory_stats_subtree(directory)
        return

    scanned = scan_directory_entries(path)
    if scanned is None:
        # Became unreadable between the caller's own check and here (a
        # permissions change, a race with a delete) — leave whatever row
        # already exists alone; it's a stale lower bound, not information
        # to actively delete.
        logger.warning('Could not read directory "%s" while refreshing its status', directory)
        return
    own_media, subdirs = scanned
    subdir_dirs = {str(s) for s in subdirs}

    own_tracked = db.count_tracked_files_by_directory([directory]).get(directory, 0)

    child_rows = db.get_directory_stats_children(directory)
    missing = [r['directory'] for r in child_rows if r['directory'] not in subdir_dirs]
    if missing:
        # A child directory this row still remembers is gone from disk —
        # drop it and everything that was below it (prefix delete), the
        # same cleanup a census does for a whole tree at once.
        for m in missing:
            db.delete_directory_stats_subtree(m)
    rows_by_dir = {r['directory']: r for r in child_rows if r['directory'] in subdir_dirs}

    subtree_media = own_media
    subtree_tracked = min(own_tracked, own_media)
    complete = True
    for sub in subdir_dirs:
        row = rows_by_dir.get(sub)
        if row is None:
            # No row yet for this subdirectory (never walked) — unknown,
            # not zero: it contributes nothing to the sum (we don't know
            # what's in it) and makes this directory incomplete.
            complete = False
            continue
        subtree_media += row['subtree_media_files'] or 0
        subtree_tracked += row['subtree_tracked_files'] or 0
        complete = complete and bool(row['subtree_complete'])

    db.upsert_directory_stats([{
        'directory': directory,
        'parent': str(path.parent),
        'media_files': own_media,
        'tracked_files': own_tracked,
        'subtree_media_files': subtree_media,
        'subtree_tracked_files': subtree_tracked,
        'subtree_complete': complete,
        'walked_at': datetime.now(timezone.utc),
        'source': source,
    }])


def refresh_chain(directory: str, source: str) -> None:
    """`refresh_directory(directory, source)`, then every ancestor up to and
    including `ROOT_DIR` — never above it, even if `directory` somehow isn't
    under `ROOT_DIR` at all (defensive only; every real caller always
    passes a path under it). Depth is small (a handful of path segments),
    so this is cheap even though it's called after every single scan unit,
    fileops operation, or tracked file."""
    root = Path(Environment().get_root_dir()).resolve()
    current = Path(directory).resolve()
    refresh_directory(str(current), source)
    if current == root:
        return
    for ancestor in current.parents:
        if not ancestor.is_relative_to(root):
            break
        refresh_directory(str(ancestor), source)
        if ancestor == root:
            break


def run_census(path: str, report: Callable[[str], None]) -> None:
    """`POST /tracking/census` (#139) — walks the whole tree at `path` in
    one pass (`scanner/walker.py::walk_directories_census`), computes every
    directory's `subtree_*`/`subtree_complete` bottom-up in memory (a
    directory's own tracked count comes from one chunked
    `count_tracked_files_by_directory` query covering every directory
    walked, not one query each), bulk-upserts the whole walked set, deletes
    any row still recorded under `path` for a directory the walk didn't see
    (gone since the last census), then refreshes the chain *above* `path`
    once — everything below `path` was just computed directly, so only the
    ancestors need the usual roll-up. Reports progress through `report` the
    same way every other TaskManager task does."""
    source = 'census'
    root_for_stats = Path(path).resolve()

    report('Walking the directory tree…')
    entries = walk_directories_census(root_for_stats)
    total = len(entries)
    noun = 'directory' if total == 1 else 'directories'
    report(f'Walked {total} {noun} — computing status…')

    db = Database()
    directories = [e.directory for e in entries]
    tracked_counts: dict[str, int] = {}
    for i in range(0, len(directories), 1000):
        tracked_counts.update(db.count_tracked_files_by_directory(directories[i:i + 1000]))

    # Bottom-up: `entries` is a pre-order walk (a directory is appended
    # before its children are visited), so iterating it in REVERSE visits
    # every descendant of a subtree before that subtree's own root — each
    # directory's children are already in `computed` by the time it's its
    # own turn.
    computed: dict[str, dict] = {}
    for e in reversed(entries):
        own_tracked = tracked_counts.get(e.directory, 0)
        subtree_media = e.media_file_count
        subtree_tracked = min(own_tracked, e.media_file_count)
        complete = True
        for sub in e.subdirectories:
            child = computed.get(sub)
            if child is None:
                complete = False
                continue
            subtree_media += child['subtree_media_files']
            subtree_tracked += child['subtree_tracked_files']
            complete = complete and child['subtree_complete']
        computed[e.directory] = {
            'subtree_media_files': subtree_media,
            'subtree_tracked_files': subtree_tracked,
            'subtree_complete': complete,
        }

    now = datetime.now(timezone.utc)
    rows = [{
        'directory': e.directory,
        'parent': e.parent if e.parent is not None else str(Path(e.directory).parent),
        'media_files': e.media_file_count,
        'tracked_files': tracked_counts.get(e.directory, 0),
        'subtree_media_files': computed[e.directory]['subtree_media_files'],
        'subtree_tracked_files': computed[e.directory]['subtree_tracked_files'],
        'subtree_complete': computed[e.directory]['subtree_complete'],
        'walked_at': now,
        'source': source,
    } for e in entries]

    report(f'Saving status for {total} {noun}…')
    seen = {e.directory for e in entries}
    with _lock:
        db.upsert_directory_stats(rows)
        existing = db.get_directory_stats_directories_under(str(root_for_stats))
        stale = list(existing - seen)
        if stale:
            db.delete_directory_stats(stale)

    env_root = Path(Environment().get_root_dir()).resolve()
    if root_for_stats != env_root:
        report('Updating parent folders…')
        refresh_chain(str(root_for_stats.parent), source)

    report(f'Updated status for {total} {noun}.')
