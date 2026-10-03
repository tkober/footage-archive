"""Rediscover-scan: reconcile a scanned folder's files against the DB by MD5.

``classify`` is a pure function: given every ``ScanResult`` produced by
scanning a folder plus the DB's known (directory, file_name) for the
relevant hashes, it decides per hash whether it is unchanged, was uniquely
relinked to a new path, is unknown ('new'), or is in conflict with one or
more on-disk copies. It touches neither the DB nor the filesystem, so it is
trivially unit-testable and reusable by #26 (a normal scan needs the exact
same rules to decide whether an upsert would silently steal a path).

``apply`` carries a ``ClassificationResult`` out: relinks update
``Files.directory``/``file_name``/``file_extension`` in one transaction,
conflicts are persisted to ``PathConflicts`` (deduplicated via
``ON CONFLICT DO NOTHING``), and — if ``track_new`` is set — new hashes are
tracked through the caller-supplied ``track_new_files`` callback (which owns
the probing/FFmpeg/exif side of tracking; this module stays free of those
dependencies). Metadata tables (FileDetails, Keywords, Location, Lists,
VideoDetails/PhotoDetails) and the filesystem are never touched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from db.database import Database
from scanner.scanner import ScanResult


def _join(directory: str, file_name: str) -> str:
    return f'{directory}/{file_name}'


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@dataclass
class Relink:
    md5_hash: str
    old_path: str
    new_path: str


@dataclass
class Conflict:
    md5_hash: str
    candidate_paths: list[str]


@dataclass
class ClassificationResult:
    unchanged: list[str] = field(default_factory=list)
    relinked: list[Relink] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    new: dict[str, list[str]] = field(default_factory=dict)


def classify(scan_results: list[ScanResult], tracked: dict[str, dict],
             exists: Callable[[str], bool]) -> ClassificationResult:
    """Classify every hash found by this scan against the DB's known rows.

    - ``scan_results``: every ``ScanResult`` produced by scanning the folder.
      Several entries may share a hash if it was found at several paths.
    - ``tracked``: md5_hash -> {'directory', 'file_name'} for the DB rows
      that matter here — at minimum, every hash present in ``scan_results``.
      A hash absent from ``tracked`` is treated as unknown ('new').
    - ``exists``: injectable filesystem check, so tests don't need real
      files on disk.

    Rules, per hash, with ``found`` = sorted paths where the hash was found
    in this scan, ``tracked_path`` = the DB's current path or None:

    1. ``tracked_path is None`` -> new.
    2. ``tracked_path in found``: unchanged; AND a conflict if other copies
       of the tracked file were also found (candidates = found - tracked).
    3. ``tracked_path not in found`` and the old path still exists on disk ->
       conflict (candidates = found); nothing changes.
    4. ``tracked_path not in found``, old path gone, exactly one path found ->
       relink.
    5. ``tracked_path not in found``, old path gone, more than one path
       found -> conflict (candidates = found).
    """
    found_by_hash: dict[str, set[str]] = {}
    for sc in scan_results:
        found_by_hash.setdefault(sc.md5_hash, set()).add(_join(sc.directory, sc.file_name))

    result = ClassificationResult()
    for md5_hash, found_set in found_by_hash.items():
        found = sorted(found_set)
        row = tracked.get(md5_hash)
        tracked_path = _join(row['directory'], row['file_name']) if row else None

        if tracked_path is None:
            result.new[md5_hash] = found
            continue

        if tracked_path in found_set:
            result.unchanged.append(md5_hash)
            others = [p for p in found if p != tracked_path]
            if others:
                result.conflicts.append(Conflict(md5_hash=md5_hash, candidate_paths=others))
            continue

        if exists(tracked_path):
            result.conflicts.append(Conflict(md5_hash=md5_hash, candidate_paths=found))
            continue

        if len(found) == 1:
            result.relinked.append(Relink(md5_hash=md5_hash, old_path=tracked_path, new_path=found[0]))
        else:
            result.conflicts.append(Conflict(md5_hash=md5_hash, candidate_paths=found))

    return result


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    unchanged: int = 0
    relinked: int = 0
    conflicts: int = 0
    new_found: int = 0
    new_tracked: int = 0
    pruned_conflicts: int = 0


def apply(classification: ClassificationResult, scan_results: list[ScanResult], db: Database,
          scanned_directory: str, track_new: bool,
          track_new_files: Optional[Callable[[list[ScanResult]], None]] = None,
          source: str = 'rediscover',
          exists: Callable[[str], bool] = lambda p: Path(p).exists()) -> ApplyResult:
    """Apply a ``ClassificationResult`` and return counts for the summary.

    - Relinks are applied as one DB transaction.
    - Conflicts (explicit ones from ``classification``, plus the losing
      copies of any newly-tracked hash found at several paths) are persisted
      with ``source`` (e.g. 'rediscover' or 'scan').
    - If ``track_new`` is set, for each unknown hash the first (sorted) found
      path is tracked via ``track_new_files``; any other paths for that hash
      become conflicts against it. With ``track_new`` unset, new hashes are
      only counted.
    - Finally, stale PathConflicts rows are pruned: a candidate is removed if
      it no longer exists on disk, or if it now equals the hash's current
      tracked path. Pruning is scoped to hashes touched by this run plus any
      existing conflict rows under ``scanned_directory`` — see
      ``Database.get_path_conflicts_for_pruning``.
    """
    result = ApplyResult(
        unchanged=len(classification.unchanged),
        relinked=len(classification.relinked),
        conflicts=len(classification.conflicts),
        new_found=len(classification.new),
    )

    if classification.relinked:
        relinks = []
        for r in classification.relinked:
            p = Path(r.new_path)
            old = Path(r.old_path)
            relinks.append({
                'md5_hash': r.md5_hash,
                'old_directory': str(old.parent),
                'old_file_name': old.name,
                'directory': str(p.parent),
                'file_name': p.name,
                'file_extension': p.suffix,
            })
        db.relink_files(relinks)

    conflict_rows = [
        {'md5_hash': c.md5_hash, 'candidate_path': candidate}
        for c in classification.conflicts
        for candidate in c.candidate_paths
    ]

    new_hashes = sorted(classification.new.keys())
    if track_new and new_hashes:
        by_hash: dict[str, list[ScanResult]] = {}
        for sc in scan_results:
            by_hash.setdefault(sc.md5_hash, []).append(sc)

        to_track: list[ScanResult] = []
        for md5_hash in new_hashes:
            found_paths = classification.new[md5_hash]  # already sorted by classify()
            first_path = found_paths[0]
            sc = next(sc for sc in by_hash[md5_hash]
                      if _join(sc.directory, sc.file_name) == first_path)
            to_track.append(sc)
            for other in found_paths[1:]:
                conflict_rows.append({'md5_hash': md5_hash, 'candidate_path': other})

        if track_new_files is not None:
            track_new_files(to_track)
        result.new_tracked = len(to_track)

    if conflict_rows:
        db.insert_path_conflicts(conflict_rows, source=source)

    touched_hashes = (
        list(classification.unchanged)
        + [r.md5_hash for r in classification.relinked]
        + [c.md5_hash for c in classification.conflicts]
        + new_hashes
    )
    result.pruned_conflicts = _prune_conflicts(db, touched_hashes, scanned_directory, exists)

    return result


def _prune_conflicts(db: Database, touched_hashes: list[str], scanned_directory: str,
                      exists: Callable[[str], bool]) -> int:
    rows = db.get_path_conflicts_for_pruning(touched_hashes, scanned_directory)
    if not rows:
        return 0

    hashes_in_rows = sorted({row['md5_hash'] for row in rows})
    tracked = db.get_tracked_paths_for_hashes(hashes_in_rows)

    to_delete = []
    for row in rows:
        md5_hash = row['md5_hash']
        candidate_path = row['candidate_path']
        tracked_row = tracked.get(md5_hash)
        current_tracked_path = (
            _join(tracked_row['directory'], tracked_row['file_name']) if tracked_row else None
        )
        if candidate_path == current_tracked_path or not exists(candidate_path):
            to_delete.append((md5_hash, candidate_path))

    return db.delete_path_conflicts(to_delete)
