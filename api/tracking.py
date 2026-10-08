import logging
import os
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Callable, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, BackgroundTasks, Response

from api.dtos import (
    ConflictCandidate,
    ConflictCountResponse,
    ConflictEntry,
    FileQuery,
    RediscoverQuery,
    RefreshQuery,
    ResolveBatchRequest,
    ResolveBatchResponse,
    ResolveBatchStrategy,
    ResolveConflictRequest,
)
from davinci.davinciresolve import Metadata, DerivedMetadataColumns
from db.database import Database, StaleConflictError
from env.environment import Environment
from fileops.pathlocks import shared
from fileops.rediscover import apply as apply_rediscover, classify as classify_rediscover
from fileops.trash import is_in_trash
from ffmpeg.ffmpeg import FFmpegInput, FFmpeg, FFprobe, VideoProbeResult
from photos.exif import probe_photo, generate_photo_thumbnail
from scanner.media_type import classify_media_type
from scanner.scanner import Scanner, ScanCandidate, ScanResult
from tasks.preview_registry import discard as discard_pending_preview, pending_previews
from tasks.taskmanager import TaskManager, TaskRequest
from tasks.workerpool import parallel_map

TrackingApi = APIRouter(prefix='/tracking')

VIDEO_TYPES = {'video', '360_video'}
PHOTO_TYPES = {'photo', '360_photo'}


@TrackingApi.post('/scan-directory')
async def scan_directory(query: FileQuery, background_tasks: BackgroundTasks):
    path = Path(query.path)
    if not path.is_dir():
        raise HTTPException(status_code=400, detail='Provided path is not a directory')
    if is_in_trash(path):
        raise HTTPException(status_code=400, detail='Path is inside the trash and cannot be scanned')

    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Scan directory',
            description=f'Scanning directory "{query.path}".',
            method=lambda report: index_files_in_directory(query, report)
        ),
        background_tasks
    )

    return task.id


@TrackingApi.post('/rediscover')
async def rediscover(query: RediscoverQuery, background_tasks: BackgroundTasks):
    path = Path(query.path)

    root = Path(Environment().get_root_dir())
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=403, detail='Access outside root directory is not allowed')
    if not path.is_dir():
        raise HTTPException(status_code=400, detail='Provided path is not a directory')
    if is_in_trash(resolved):
        raise HTTPException(status_code=400, detail='Path is inside the trash and cannot be rediscovered')

    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Rediscover',
            description=f'Rediscovering directory "{query.path}".',
            method=lambda report: rediscover_directory(query, report)
        ),
        background_tasks
    )

    return task.id


def _conflict_entries(md5_hashes: list[str], conflict_rows: list[dict]) -> list[ConflictEntry]:
    """Build ConflictEntry objects for ``md5_hashes`` from a flat list of
    PathConflicts rows (as returned by Database.get_path_conflicts). One
    summary query (get_tracked_files_with_attachment_counts) plus os.path
    existence checks — no further DB round-trips."""
    if not md5_hashes:
        return []

    summaries = {s['md5_hash']: s for s in Database().get_tracked_files_with_attachment_counts(
        md5_hashes=md5_hashes
    )}

    candidates_by_hash: dict[str, list[dict]] = {}
    for row in conflict_rows:
        candidates_by_hash.setdefault(row['md5_hash'], []).append(row)

    entries = []
    for md5_hash in md5_hashes:
        summary = summaries.get(md5_hash)
        if summary is None:
            # Hash no longer tracked (FK cascade would also have removed its
            # conflicts, but guard against a race anyway).
            continue
        tracked_path = f"{summary['directory']}/{summary['file_name']}"
        candidates = [
            ConflictCandidate(
                path=row['candidate_path'],
                exists=Path(row['candidate_path']).exists(),
                source=row['source'],
                found_at=row['found_at'],
            )
            for row in sorted(candidates_by_hash.get(md5_hash, []), key=lambda r: r['candidate_path'])
        ]
        entries.append(ConflictEntry(
            md5_hash=md5_hash,
            file_name=summary['file_name'],
            media_type=summary['media_type'],
            has_preview=summary['has_preview'],
            keyword_count=summary['keyword_count'],
            has_location=summary['has_location'],
            list_count=summary['list_count'],
            tracked_path=tracked_path,
            tracked_exists=Path(tracked_path).exists(),
            candidates=candidates,
        ))

    entries.sort(key=lambda e: e.tracked_path)
    return entries


@TrackingApi.get('/conflicts')
async def get_conflicts() -> list[ConflictEntry]:
    db = Database()
    md5_hashes = db.get_distinct_conflict_hashes()
    rows = db.get_path_conflicts()
    return _conflict_entries(md5_hashes, rows)


@TrackingApi.get('/conflicts/count')
async def get_conflicts_count() -> ConflictCountResponse:
    return ConflictCountResponse(count=Database().count_distinct_conflicts())


@TrackingApi.post('/conflicts/resolve', status_code=204)
async def resolve_conflict(query: ResolveConflictRequest):
    db = Database()
    rows = db.get_path_conflicts(query.md5_hash)
    if not rows:
        raise HTTPException(status_code=404, detail='No open conflicts for this hash')

    tracked = db.get_tracked_paths_for_hashes([query.md5_hash]).get(query.md5_hash)
    if tracked is None:
        raise HTTPException(status_code=404, detail='File is no longer tracked')
    tracked_path = f"{tracked['directory']}/{tracked['file_name']}"

    valid_paths = {tracked_path} | {r['candidate_path'] for r in rows}
    if query.chosen_path not in valid_paths:
        raise HTTPException(
            status_code=400,
            detail='chosen_path must be the currently tracked path or one of its candidates',
        )

    root = Path(Environment().get_root_dir())
    if not Path(query.chosen_path).resolve().is_relative_to(root):
        raise HTTPException(status_code=403, detail='Access outside root directory is not allowed')
    if not Path(query.chosen_path).exists():
        raise HTTPException(status_code=409, detail='File no longer exists')

    new_directory = new_file_name = new_file_extension = None
    if query.chosen_path != tracked_path:
        p = Path(query.chosen_path)
        new_directory, new_file_name, new_file_extension = str(p.parent), p.name, p.suffix

    try:
        db.resolve_path_conflict(
            query.md5_hash, tracked['directory'], tracked['file_name'],
            new_directory, new_file_name, new_file_extension,
        )
    except StaleConflictError:
        raise HTTPException(status_code=409, detail='The tracked path changed meanwhile, please reload')
    return Response(status_code=204)


@TrackingApi.post('/conflicts/resolve-batch')
async def resolve_conflicts_batch(query: ResolveBatchRequest) -> ResolveBatchResponse:
    db = Database()
    resolved = 0
    skipped = []

    for md5_hash in query.md5_hashes:
        rows = db.get_path_conflicts(md5_hash)
        if not rows:
            skipped.append({'md5_hash': md5_hash, 'reason': 'No open conflicts'})
            continue

        tracked = db.get_tracked_paths_for_hashes([md5_hash]).get(md5_hash)
        if tracked is None:
            skipped.append({'md5_hash': md5_hash, 'reason': 'File is no longer tracked'})
            continue
        tracked_path = f"{tracked['directory']}/{tracked['file_name']}"

        if query.strategy == ResolveBatchStrategy.KEEP_TRACKED:
            if not Path(tracked_path).exists():
                skipped.append({'md5_hash': md5_hash, 'reason': 'Tracked path no longer exists'})
                continue
            db.resolve_path_conflict(md5_hash, tracked['directory'], tracked['file_name'])
            resolved += 1
        else:
            existing = [r['candidate_path'] for r in rows if Path(r['candidate_path']).exists()]
            if not existing:
                skipped.append({'md5_hash': md5_hash, 'reason': 'No existing candidate'})
                continue
            if len(existing) > 1:
                skipped.append({'md5_hash': md5_hash, 'reason': 'Ambiguous: multiple existing candidates'})
                continue
            p = Path(existing[0])
            try:
                db.resolve_path_conflict(
                    md5_hash, tracked['directory'], tracked['file_name'],
                    str(p.parent), p.name, p.suffix,
                )
            except StaleConflictError:
                skipped.append({'md5_hash': md5_hash, 'reason': 'Tracked path changed meanwhile'})
                continue
            resolved += 1

    return ResolveBatchResponse(resolved=resolved, skipped=skipped)


@TrackingApi.post('/scan-file')
async def scan_file(query: FileQuery, background_tasks: BackgroundTasks):
    path = Path(query.path)
    if path.is_dir():
        raise HTTPException(status_code=400, detail='Provided path is a directory')
    if not path.exists():
        raise HTTPException(status_code=404, detail='File not found')
    if is_in_trash(path):
        raise HTTPException(status_code=400, detail='Path is inside the trash and cannot be tracked')

    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Track file',
            description=f'Tracking file "{query.path}".',
            method=lambda report: index_single_file(query, report)
        ),
        background_tasks
    )

    return task.id


@TrackingApi.post('/refresh')
async def refresh(query: RefreshQuery, background_tasks: BackgroundTasks):
    if not query.md5_hashes:
        raise HTTPException(status_code=400, detail='No files to rescan')

    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Rescan files',
            description=f"Rescanning {len(query.md5_hashes)} file{'' if len(query.md5_hashes) == 1 else 's'}.",
            method=lambda report: refresh_tracked_files(query, report)
        ),
        background_tasks
    )

    return task.id


@TrackingApi.post('/import-metadata')
async def import_metadata(query: FileQuery, background_tasks: BackgroundTasks):
    path = Path(query.path)
    if path.is_dir():
        raise HTTPException(status_code=400, detail='Provided path is a directory')

    if not path.exists():
        raise HTTPException(status_code=404, detail='File not found')
    if is_in_trash(path):
        raise HTTPException(status_code=400, detail='Path is inside the trash and cannot be imported')

    task_manager = TaskManager()
    task = task_manager.request_task(
        TaskRequest(
            name='Import metadata',
            description=f'Importing metadata from "{query.path}".',
            method=lambda report: scan_files_in_metadata(query, report)
        ),
        background_tasks
    )

    return task.id


class _ProbeProgress:
    """Thread-safe completion counter — workers finish out of order, so the
    running tally is guarded by a lock and reported as '{label} done / total'
    (`label` defaults to 'Probed'; the streaming scan, #135, reuses this same
    class for the 'Hashed' counter — see `_index_candidates`)."""

    def __init__(self, total: int, report: Callable[[str], None], label: str = 'Probed'):
        self._total = total
        self._report = report
        self._label = label
        self._lock = Lock()
        self._done = 0
        self._failed = 0

    def record(self, file_name: str, ok: bool):
        # Report inside the lock so both the count and the displayed message are
        # strictly monotonic — workers finish out of order, but the lock serialises
        # each increment with the report it produces. report() is a trivial in-memory
        # assignment, so holding the lock across it is cheap.
        with self._lock:
            self._done += 1
            if not ok:
                self._failed += 1
            suffix = f' ({self._failed} failed)' if self._failed else ''
            self._report(f'{self._label} {self._done} / {self._total}{suffix}: {file_name}')

    @property
    def done(self) -> int:
        with self._lock:
            return self._done

    def skip(self, count: int, failed: int = 0):
        """Advance the counter for `count` candidates that will never
        individually reach `record()` — e.g. a streaming scan's batch (#135)
        that ended with some candidates left unprobed (classified as a
        conflict) or never hashed at all (failed to hash). Keeps 'Probed
        x / y' monotonic and able to reach `y` even then. A no-op for
        `count <= 0`, so a batch with nothing left over doesn't emit an
        empty-looking message."""
        if count <= 0:
            return
        with self._lock:
            self._done += count
            self._failed += failed
            suffix = f' ({self._failed} failed)' if self._failed else ''
            self._report(f'{self._label} {self._done} / {self._total}{suffix}')


@dataclass
class ScanSummary:
    """Counts for one streaming scan's worth of reconciliation (#135) — a
    single call for `index_single_file`, or summed across every batch for
    `index_files_in_directory` via `_index_candidates` — so the final
    "Indexed N files · M skipped unchanged · K relinked · J conflicts[ · F
    failed]" message can be composed once the whole scan (every batch) is
    done, not per batch. `skipped` (#136) is only ever set once, up front,
    by `_index_candidates`'s incremental skip rule — no batch contributes to
    it, but it rides along through `__add__` like every other field.

    `cancelled` (#137) is set True by `_index_candidates` once it stops
    early because `should_cancel()` returned True — the scan queue's unit
    executor (`run_scan_unit`) reads it to decide DONE vs. CANCELLED.
    Deliberately NOT summed by `__add__` (only `_index_candidates` itself
    ever sets it, on the summary it returns — a batch's own ScanSummary
    never carries it)."""
    indexed: int = 0
    relinked: int = 0
    conflicts: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: bool = False

    def __add__(self, other: 'ScanSummary') -> 'ScanSummary':
        return ScanSummary(
            indexed=self.indexed + other.indexed,
            relinked=self.relinked + other.relinked,
            conflicts=self.conflicts + other.conflicts,
            failed=self.failed + other.failed,
            skipped=self.skipped + other.skipped,
            cancelled=self.cancelled or other.cancelled,
        )


def _format_scan_summary(summary: ScanSummary) -> str:
    message = f'Indexed {summary.indexed} files'
    if summary.skipped:
        message += f' · {summary.skipped} skipped unchanged'
    message += f' · {summary.relinked} relinked · {summary.conflicts} conflicts'
    if summary.failed:
        message += f' · {summary.failed} failed'
    return message


def _filter_unchanged_candidates(candidates: list[ScanCandidate],
                                  db: Database) -> tuple[list[ScanCandidate], int]:
    """Incremental scan skip rule (#136): drop a candidate, before it's ever
    hashed, when a `Files` row already exists at exactly its (directory,
    file_name) with both `size_bytes`/`mtime_ns` non-NULL and exactly equal
    to what `collect_candidates` just stat'd. Signatures are fetched one
    query per *directory* (`Database.get_file_signatures_in_directory`), not
    per file or per batch, by grouping candidates on their parent directory
    first.

    `Files` has no uniqueness on (directory, file_name) — a file whose
    content changed is a new hash, and the old row can keep pointing at the
    same path — so a candidate matches if ANY row for that path carries
    exactly its signature, not just one.

    Known gap (documented in CLAUDE.md too): a file replaced by a different
    one with the same size AND mtime (e.g. a `cp -p` over it) is skipped
    like any other unchanged file. `force_rehash=True` is the escape hatch.

    Returns the surviving candidates plus how many were skipped."""
    by_directory: dict[str, list[ScanCandidate]] = {}
    for c in candidates:
        by_directory.setdefault(str(c.path.parent), []).append(c)

    kept: list[ScanCandidate] = []
    skipped = 0
    for directory, dir_candidates in by_directory.items():
        signatures = db.get_file_signatures_in_directory(directory)
        for c in dir_candidates:
            known = signatures.get(c.path.name)
            if known and (c.size_bytes, c.mtime_ns) in known:
                skipped += 1
                continue
            kept.append(c)
    return kept, skipped


def _save_signatures(db: Database, scan_results: list[ScanResult]) -> None:
    """Persist `Files.size_bytes`/`mtime_ns` (#136) for every `ScanResult`
    whose probe step just finished *without raising* — see the CRASH-SAFETY
    note on `_scan_and_reconcile` for why this must only ever run after a
    successful probe, never from the pre-probe `insert_scan_results` upsert.
    A `ScanResult` with no signature of its own (`refresh_tracked_files`
    builds one straight from an existing `Files` row, never through this
    path) is skipped rather than writing NULLs over a real signature."""
    rows = [
        {'md5_hash': sc.md5_hash, 'directory': sc.directory, 'file_name': sc.file_name,
         'size_bytes': sc.size_bytes, 'mtime_ns': sc.mtime_ns}
        for sc in scan_results
        if sc.size_bytes is not None and sc.mtime_ns is not None
    ]
    db.set_file_signatures(rows)


def _index_candidates(candidates: list[ScanCandidate], db: Database, report: Callable[[str], None],
                       generate_clip_preview: bool, scanned_directory: str,
                       force_rehash: bool = False,
                       should_cancel: Optional[Callable[[], bool]] = None) -> ScanSummary:
    """Streaming scan (#135): hash + reconcile `candidates` in batches of
    `SCAN_BATCH_SIZE` (env, default 25) instead of over the whole tree at
    once, so early batches land in the DB — and show in the browser — while
    later ones are still being hashed. Factored out of
    `index_files_in_directory` so a later ticket (#137, a persistent queue
    whose unit is one directory's direct files, no recursion) can call it
    the same way.

    Incremental skip rule (#136, `force_rehash=False` the default): before
    anything is batched, `_filter_unchanged_candidates` drops any candidate
    already tracked at exactly its path with a matching size+mtime
    signature — it's never hashed, never probed, and `last_indexed_at` is
    never bumped. `force_rehash=True` (the context menu's "Scan folder
    (force rehash)") skips this entirely, hashing/probing every candidate.
    The `_ProbeProgress` totals below are sized off the *post-skip* count,
    so "Hashed x / n"/"Probed x / n" only count files actually processed.

    Candidates are sorted alphabetically once, up front, before batching —
    not per batch — so the "first sorted path wins" duplicate rule
    (`fileops/rediscover.py::classify()`, rule 3) resolves the same way
    regardless of batch size: a duplicate's alphabetically-first copy is
    always in the same batch as, or an earlier batch than, any other copy.
    See CLAUDE.md's scan flow section for the one behaviour difference this
    still leaves vs. a single-batch scan (a tracked hash relinked to one of
    two new copies found in two different batches).

    One `_ProbeProgress` each for 'Hashed'/'Probed' spans the whole scan
    (total = the post-skip candidate count), not just one batch, so both
    stay monotonic across every batch. A candidate that ends this batch
    unprobed — a conflict, or a hash failure — still advances the Probed
    counter once the batch is done (`_ProbeProgress.skip`), so it can still
    reach its total even though it never calls `record()` itself.

    Failure isolation: a candidate that can't be hashed (`OSError` —
    vanished, permission, I/O) is logged and counted as failed, the rest of
    its batch still hashing (`Scanner.hash_candidates(isolate_errors=True)`).
    An unexpected exception while reconciling a batch is logged and every
    one of that batch's hashed files counts as failed — but the next batch
    still runs, unlike the whole-tree scan this replaces, where a single bad
    file today aborts everything via `parallel_map`.

    `should_cancel` (#137, the scan queue's cooperative cancellation —
    None for every caller except `run_scan_unit`) is checked once before
    each batch starts (no new batch once it returns True — the loop just
    stops, leaving `ScanSummary.cancelled` True on what's returned) and
    threaded into `Scanner.hash_candidates` (checked once per candidate
    before it's hashed) and `_scan_and_reconcile`'s per-file probe step —
    see their own docstrings. Files already hashed/probed before
    cancellation stay tracked; a candidate skipped because of it is simply
    never processed, same as one that vanished mid-scan."""
    if not candidates:
        return ScanSummary()

    skipped = 0
    if not force_rehash:
        candidates, skipped = _filter_unchanged_candidates(candidates, db)

    total = len(candidates)
    if total == 0:
        return ScanSummary(skipped=skipped)

    sorted_candidates = sorted(candidates, key=lambda c: str(c.path))
    batch_size = Environment().get_scan_batch_size()

    scanner = Scanner()
    hashed_progress = _ProbeProgress(total, report, label='Hashed')
    probed_progress = _ProbeProgress(total, report, label='Probed')

    hash_failures = [0]

    def hash_progress(path: Path, ok: bool):
        if not ok:
            hash_failures[0] += 1
        hashed_progress.record(path.name, ok)

    summary = ScanSummary(skipped=skipped)
    for start in range(0, total, batch_size):
        if should_cancel is not None and should_cancel():
            summary.cancelled = True
            break

        batch = sorted_candidates[start:start + batch_size]
        failures_before = hash_failures[0]
        scan_results = scanner.hash_candidates(batch, progress=hash_progress, isolate_errors=True,
                                                should_cancel=should_cancel)
        # Only real hash errors count as failed; candidates left out because
        # of a cancel were never processed (#137).
        hash_failed = hash_failures[0] - failures_before

        probed_before = probed_progress.done
        try:
            batch_summary = _scan_and_reconcile(
                scan_results, db, report, generate_clip_preview, scanned_directory,
                progress=probed_progress, should_cancel=should_cancel,
            ) + ScanSummary(failed=hash_failed)
            not_probed, not_probed_failed = len(batch) - batch_summary.indexed, hash_failed
        except Exception:
            logging.exception(f'Failed to reconcile a batch of {len(scan_results)} files')
            batch_summary = ScanSummary(failed=len(batch))
            # Some of the batch may already have been probed (and counted)
            # before the exception, so only top up what's still open.
            not_probed = len(batch) - (probed_progress.done - probed_before)
            not_probed_failed = not_probed

        probed_progress.skip(not_probed, failed=not_probed_failed)
        summary += batch_summary

        # A cancel that hit this batch (even the last one) must end the scan
        # as cancelled, not as a normal finish (#137).
        if should_cancel is not None and should_cancel():
            summary.cancelled = True
            break

    return summary


def index_files_in_directory(query: FileQuery, report: Callable[[str], None]):
    directory = Path(query.path)
    with shared(str(directory)):
        report('Scanning files…')
        candidates = Scanner().collect_candidates(directory.rglob('*'))
        report(f'Found {len(candidates)} files, hashing…')
        db = Database()
        summary = _index_candidates(candidates, db, report,
                                     generate_clip_preview=query.generate_clip_preview,
                                     scanned_directory=str(directory),
                                     force_rehash=query.force_rehash)
        report(_format_scan_summary(summary))


def run_scan_unit(directory: str, options: dict, report: Callable[[str], None],
                  should_cancel: Callable[[], bool]) -> ScanSummary:
    """Executor of one ScanUnit (#137's persistent scan queue — see
    `tasks/scanqueue.py`): the direct, non-recursive counterpart of
    `index_files_in_directory` for a single directory. `options` is the
    job's `ScanJobs.options` dict (`generate_clip_preview`/`force_rehash`,
    same meaning as on `FileQuery`); `should_cancel` is threaded straight
    into `_index_candidates`.

    Candidates are DIRECT files only (`os.scandir`, no recursion — a unit
    never walks into a subdirectory, unlike `index_files_in_directory`'s
    `rglob('*')`), filtered the same way by `Scanner.collect_candidates`."""
    path = Path(directory)
    with shared(str(path)):
        try:
            entries = list(os.scandir(path))
        except OSError as e:
            raise RuntimeError(f'Could not read directory "{directory}": {e}') from e
        direct_files = (Path(e.path) for e in entries if e.is_file(follow_symlinks=False))
        candidates = Scanner().collect_candidates(direct_files)
        db = Database()
        summary = _index_candidates(
            candidates, db, report,
            generate_clip_preview=options.get('generate_clip_preview', True),
            scanned_directory=str(path),
            force_rehash=options.get('force_rehash', False),
            should_cancel=should_cancel,
        )
        report(_format_scan_summary(summary))
        return summary


def rediscover_directory(query: RediscoverQuery, report: Callable[[str], None]):
    directory = Path(query.path)
    with shared(str(directory)):
        scanner = Scanner()
        candidates = scanner.collect_candidates(directory.rglob('*'))
        total = len(candidates)
        # Progress during hashing (#135) — rediscover still hashes the whole
        # tree in one go (its classification needs the complete hash set,
        # see the module docstring), so no batching/isolation here, just the
        # 'Hashed x / y' counter.
        hashed_progress = _ProbeProgress(total, report, label='Hashed')
        scan_results = scanner.hash_candidates(
            candidates, progress=lambda p, ok: hashed_progress.record(p.name, ok),
        )
        report(f'Hashed {total} files, matching against database…')

        db = Database()
        md5_hashes = sorted({sc.md5_hash for sc in scan_results})
        tracked = db.get_tracked_paths_for_hashes(md5_hashes)
        classification = classify_rediscover(scan_results, tracked, exists=lambda p: Path(p).exists())

        def track_new_files(to_track: list[ScanResult]):
            if not to_track:
                return
            db.insert_scan_results(to_track)
            progress = _ProbeProgress(len(to_track), report)

            def probe(sc: ScanResult) -> bool:
                ok = True
                try:
                    _probe_and_save(sc, db, generate_clip_preview=query.generate_clip_preview)
                except Exception:
                    logging.exception(f'Failed to probe {sc.directory}/{sc.file_name}')
                    ok = False
                finally:
                    progress.record(sc.file_name, ok)
                return ok

            # Registered for just the hashes about to be probed here, not the
            # whole rediscover (`unchanged`/`relinked` hashes aren't probed by
            # a rediscover at all), and only when a preview is actually
            # requested (#77).
            ctx = pending_previews(sc.md5_hash for sc in to_track) if query.generate_clip_preview else nullcontext()
            with ctx:
                oks = parallel_map(to_track, probe)
            # Newly-tracked files get their size+mtime signature too (#136),
            # same as a normal scan's new hashes — so a later incremental
            # scan of this folder can skip them. Only hashes/relinks
            # rediscover applies without probing (unchanged/relinked) never
            # get here, which is fine: their signature (if any) is untouched.
            _save_signatures(db, [sc for sc, ok in zip(to_track, oks) if ok])

        report('Applying changes…')
        result = apply_rediscover(
            classification, scan_results, db,
            scanned_directory=str(directory),
            track_new=query.track_new,
            track_new_files=track_new_files if query.track_new else None,
            source='rediscover',
        )

        report(_rediscover_summary(result, query.track_new))


def _rediscover_summary(result, track_new: bool) -> str:
    new_label = f'{result.new_tracked} new tracked' if track_new else f'{result.new_found} new (not tracked)'
    return (
        f'{result.relinked} relinked · {result.conflicts} conflicts · '
        f'{new_label} · {result.unchanged} unchanged'
    )


def _scan_and_reconcile(scan_results: list[ScanResult], db: Database, report: Callable[[str], None],
                        generate_clip_preview: bool, scanned_directory: str,
                        progress: Optional[_ProbeProgress] = None,
                        should_cancel: Optional[Callable[[], bool]] = None) -> ScanSummary:
    """Shared reconciliation path for the normal scan (directory + single
    file): classify every hash found against the DB using the same rules as
    `/tracking/rediscover` (fileops/rediscover.py), then probe every hash
    that ends up at a settled tracked path — new (first sorted path wins,
    other copies become conflicts), unchanged (re-probed to bump
    last_indexed_at), or relinked (tracked path was gone, found exactly
    once, so it was relinked like a rediscover before probing at the new
    path). A hash left in conflict (old path still exists, or found more
    than once with the old path gone) is never inserted/probed and its
    `Files` row is left untouched.

    Returns its counts as a `ScanSummary` instead of reporting the final
    "Indexed …" message itself (#135), so a caller that reconciles several
    batches (`_index_candidates`) can sum them and report just once for the
    whole scan; `index_single_file` (one call = the whole scan) reports its
    single `ScanSummary` right away. `progress`, if given, is the caller's
    own shared 'Probed x / y' counter spanning every batch — otherwise
    (e.g. `index_single_file`) one is created here, scoped to just this
    call, as before #135.

    CRASH-SAFETY (#136, relied on by a later ticket): `probe_batch` writes
    each batch's signatures (`Files.size_bytes`/`mtime_ns`, via
    `_save_signatures`/`Database.set_file_signatures`) only *after* every
    file in it has finished probing — a recorded ffprobe failure that
    returns normally still counts as finished, only an exception doesn't.
    If the process dies (or the scan is cancelled) between `insert_scan_results`
    and a file's probe completing, that file keeps a NULL (or stale,
    now-mismatching) signature, so the next incremental scan re-hashes and
    re-probes it instead of skipping it forever."""
    md5_hashes = sorted({sc.md5_hash for sc in scan_results})
    tracked = db.get_tracked_paths_for_hashes(md5_hashes)
    report('Matching against database…')
    classification = classify_rediscover(scan_results, tracked, exists=lambda p: Path(p).exists())

    scan_results_by_path = {f'{sc.directory}/{sc.file_name}': sc for sc in scan_results}

    # One shared progress counter across both probing phases below (new
    # hashes tracked during apply_rediscover(), then unchanged/relinked
    # hashes probed at their settled path afterwards) so the per-file
    # "Probed X / Y" messages stay monotonic for the whole scan.
    if progress is None:
        total_to_probe = len(classification.new) + len(classification.unchanged) + len(classification.relinked)
        progress = _ProbeProgress(total_to_probe, report)

    def probe_one(sc: ScanResult) -> bool:
        # #137: checked before the probe itself — a skipped-because-cancelled
        # file never calls _probe_and_save, so no media details/preview are
        # written and (critically) no signature gets saved for it below
        # (`probe_batch` only saves signatures for files `probe_one` returned
        # True for) — the next scan re-hashes/re-probes it, same as #136's
        # crash-safety rule for a process that died mid-probe.
        if should_cancel is not None and should_cancel():
            return False
        ok = True
        try:
            _probe_and_save(sc, db, generate_clip_preview=generate_clip_preview)
        except Exception:
            # Isolate per-file failures so one bad file doesn't abort the whole scan.
            logging.exception(f'Failed to probe {sc.directory}/{sc.file_name}')
            ok = False
        finally:
            progress.record(sc.file_name, ok)
        return ok

    def probe_batch(batch: list[ScanResult]):
        if not batch:
            return
        # `insert_scan_results` is only ever called here with ScanResults that
        # already match the (post-relink) tracked path, so this upsert can
        # never move a Files row — it only inserts new hashes or bumps
        # last_indexed_at for ones that stayed put. It also never touches
        # size_bytes/mtime_ns (#136, see Database.insert_scan_results) — only
        # the write below does, and only for files that just finished probing.
        db.insert_scan_results(batch)
        oks = parallel_map(batch, probe_one)
        _save_signatures(db, [sc for sc, ok in zip(batch, oks) if ok])

    # Every hash that's about to be probed in this scan — registered for the
    # duration of the whole reconciliation (both probe_batch() calls below),
    # and only when a preview is actually requested (#77).
    pending_ctx = pending_previews(md5_hashes) if generate_clip_preview else nullcontext()
    with pending_ctx:
        report('Applying changes…')
        result = apply_rediscover(
            classification, scan_results, db,
            scanned_directory=scanned_directory,
            track_new=True,
            track_new_files=probe_batch,
            source='scan',
        )

        settled: list[ScanResult] = []
        for md5_hash in classification.unchanged:
            row = tracked[md5_hash]
            sc = scan_results_by_path.get(f"{row['directory']}/{row['file_name']}")
            if sc is not None:
                settled.append(sc)
        for r in classification.relinked:
            sc = scan_results_by_path.get(r.new_path)
            if sc is not None:
                settled.append(sc)

        probe_batch(settled)

    indexed = result.new_tracked + len(settled)
    return ScanSummary(indexed=indexed, relinked=result.relinked, conflicts=result.conflicts)


def index_single_file(query: FileQuery, report: Callable[[str], None]):
    path = Path(query.path)
    with shared(str(path)):
        report('Hashing file…')
        scan_results = Scanner().scan_files([path])
        if not scan_results:
            return
        db = Database()
        summary = _scan_and_reconcile(scan_results, db, report,
                                       generate_clip_preview=query.generate_clip_preview,
                                       scanned_directory=str(path.parent))
        report(_format_scan_summary(summary))


def refresh_tracked_files(query: RefreshQuery, report: Callable[[str], None]):
    """POST /tracking/refresh (#64) — "rescan" already-tracked files: re-probe
    metadata and regenerate the preview for a known hash without re-hashing
    (so this is fast even for a large video over the network, and never
    changes which path/hash a file is tracked under). An unknown hash is
    silently skipped (still counted into the X/Y progress, since the
    requested total already includes it, but never into the final tally —
    it was never tracked to begin with). A missing/trashed file is counted
    as "missing" instead of being probed. Per-file failures are isolated
    like `probe_one`, and processing is fanned out across the shared worker
    pool."""
    db = Database()
    requested = list(dict.fromkeys(query.md5_hashes))
    total = len(requested)

    done = 0
    missing = 0
    failed = 0
    succeeded = 0
    without_preview = 0
    progress_lock = Lock()
    counters_lock = Lock()

    def report_progress():
        nonlocal done
        with progress_lock:
            done += 1
            report(f'Rescanned {done} / {total}')

    to_process = []
    for md5_hash in requested:
        row = db.get_file_by_hash(md5_hash)
        if row is None:
            report_progress()  # untracked — skipped silently, not tallied below
            continue

        file_path = f"{row['directory']}/{row['file_name']}"
        if not Path(file_path).exists() or is_in_trash(file_path):
            missing += 1
            report_progress()
            continue

        to_process.append(row)

    def process(row: dict):
        nonlocal succeeded, without_preview, failed
        file_path = f"{row['directory']}/{row['file_name']}"
        try:
            with shared(file_path):
                sc = ScanResult(
                    md5_hash=row['md5_hash'],
                    file_name=row['file_name'],
                    file_extension=row['file_extension'],
                    media_type=row['media_type'],
                    directory=row['directory'],
                    last_indexed_at=datetime.now(),
                )
                has_preview = _probe_and_save(sc, db, generate_clip_preview=True)
                db.touch_last_indexed_at(sc.md5_hash)
            with counters_lock:
                succeeded += 1
                if not has_preview:
                    without_preview += 1
        except Exception:
            logging.exception(f'Failed to rescan {file_path}')
            with counters_lock:
                failed += 1
        finally:
            report_progress()

    # Preview generation is always on for a rescan (see docstring above), so
    # every file about to be processed is registered for the duration (#77).
    with pending_previews(row['md5_hash'] for row in to_process):
        parallel_map(to_process, process)

    parts = [f"Rescanned {succeeded} file{'' if succeeded == 1 else 's'}"]
    if without_preview:
        parts.append(f'{without_preview} without preview')
    if missing:
        parts.append(f'{missing} missing')
    if failed:
        parts.append(f'{failed} failed')
    report(' · '.join(parts))


def _refine_media_type(sc: ScanResult, db: Database, *,
                       make: str | None = None, projection: str | None = None) -> str | None:
    """Re-derive `sc`'s media_type from its extension (via the *current*
    env map, not whatever is already stored) plus whatever metadata the
    probe above just produced (#79, `scanner/media_type.py`), and persist
    the correction if it differs from what's stored — e.g. a `.dng`
    previously (mis)classified `360_photo` by an older extension map gets
    downgraded to `photo` here once its EXIF `Make` says it isn't an
    Insta360 file, on the very next scan/rescan of it. `make`/`projection`
    are both None when the probe failed, which correctly resolves to the
    non-360 family (unknown metadata never upgrades a file to 360). Returns
    the refined value and mutates `sc.media_type` in place so every
    subsequent use in this call (generate_preview, the caller's return
    value) sees the corrected type."""
    refined = classify_media_type(
        sc.file_extension, Environment().get_media_type_map(),
        make=make, projection=projection,
    )
    if refined != sc.media_type:
        db.set_media_type(sc.md5_hash, refined)
        sc.media_type = refined
    return refined


def _probe_and_save(sc: ScanResult, db: Database, generate_clip_preview: bool) -> bool:
    """Probes `sc`'s media type and saves the resulting details. Returns
    whether the file has a preview after this call — only meaningful when
    `generate_clip_preview` is True; always False otherwise. Non-media files
    (`sc.media_type` None) never get a preview. Used by the rescan task
    (#64) to count files left "without preview" (e.g. FFprobe returned None,
    or ffmpeg/the thumbnailer silently produced nothing).

    Also the one place that refines `sc.media_type` from real metadata
    (#79, see `_refine_media_type`) instead of trusting the scanner's
    extension-only guess — before any VideoDetails/PhotoDetails insert or
    preview generation, so both use the corrected type."""
    file_path = sc.directory + '/' + sc.file_name
    last_modified_at = datetime.fromtimestamp(Path(file_path).stat().st_mtime).isoformat()

    if sc.media_type in VIDEO_TYPES:
        probe = FFprobe().probe_file(md5_hash=sc.md5_hash, file_path=file_path)
        if probe is None:
            logging.warning(f'FFprobe failed for {file_path}')
            _refine_media_type(sc, db)
            _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=None)
            if generate_clip_preview:
                # No generate_preview call on this path, so record the outcome
                # here — otherwise the file would show "missing" (#77).
                db.set_preview_status(sc.md5_hash, 'failed', 'FFprobe could not read the file')
                discard_pending_preview(sc.md5_hash)
            return False

        _refine_media_type(sc, db, projection=probe.projection)
        _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=probe.recorded_at)
        db.insert_video_details(pd.DataFrame([probe.model_dump()]))

        if generate_clip_preview:
            return generate_preview(sc.md5_hash, file_path, sc.media_type, probe=probe)
        return False

    elif sc.media_type in PHOTO_TYPES:
        probe = probe_photo(md5_hash=sc.md5_hash, file_path=file_path)
        if probe is None:
            _refine_media_type(sc, db)
            _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=None)
        else:
            _refine_media_type(sc, db, make=probe.camera_make, projection=probe.projection)
            _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=probe.recorded_at,
                               latitude=probe.latitude, longitude=probe.longitude,
                               altitude=probe.altitude)
            db.insert_photo_details(pd.DataFrame([probe.model_dump()]))

        if generate_clip_preview:
            return generate_preview(sc.md5_hash, file_path, sc.media_type)
        return False

    else:
        _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=None)
        return False


def _save_file_details(db: Database, md5_hash: str, last_modified_at: str, recorded_at: str | None,
                       latitude: float | None = None, longitude: float | None = None,
                       altitude: float | None = None):
    df = pd.DataFrame([{
        'md5_hash': md5_hash,
        'last_modified_at': last_modified_at,
        'recorded_at': recorded_at,
        'latitude': latitude,
        'longitude': longitude,
        'altitude': altitude,
    }])
    db.insert_file_details(df)


def scan_files_in_metadata(query: FileQuery, report: Callable[[str], None]):
    path = Path(query.path)
    # The CSV can reference files scattered anywhere under ROOT_DIR, not just
    # next to `path` itself, so the whole root is held as a shared lock for
    # the duration of the import rather than guessing a narrower subtree.
    with shared(Environment().get_root_dir()):
        report('Parsing metadata…')
        metadata = Metadata(path)
        records = metadata.get_details()
        keywords = metadata.get_keywords()
        total = len(records)
        report(f'Hashing {total} files…')
        scan_results = Scanner().scan_files(records[DerivedMetadataColumns.FILE_PATH.value])

        df = pd.DataFrame([r.model_dump() for r in scan_results])
        df[DerivedMetadataColumns.FILE_PATH.value] = df['directory'] + '/' + df['file_name']

        details_merged = pd.merge(
            left=df[['md5_hash', DerivedMetadataColumns.FILE_PATH.value]],
            right=records,
            on=DerivedMetadataColumns.FILE_PATH.value
        )

        keywords_merged = pd.merge(
            left=df[['md5_hash', DerivedMetadataColumns.FILE_PATH.value]],
            right=keywords,
            on=DerivedMetadataColumns.FILE_PATH.value
        )

        report('Writing to database…')
        db = Database()
        db.insert_scan_results(scan_results)
        db.insert_file_details(details_merged)
        db.insert_video_details(details_merged)
        db.insert_keywords(keywords_merged)

        if query.generate_clip_preview:
            for i, row in enumerate(details_merged.itertuples(index=True, name='Row'), 1):
                report(f'Generating preview {i} / {total}')
                ffmpeg_input = FFmpegInput.from_time_code(
                    md5_hash=row.md5_hash,
                    file_path=row.file_path,
                    duration_tc=row.duration_tc
                )
                create_clip_preview(ffmpeg_input)


def generate_preview(md5_hash: str, file_path: str, media_type: str | None,
                      probe: VideoProbeResult | None = None) -> bool:
    """Generate+store a preview for a known `media_type`, reporting whether
    one was actually produced. Shared by `_probe_and_save` (#64) and the
    missing-preview repair (#65) so there's exactly one place that decides
    how a video vs. a photo gets its preview — and, as of #77, the one place
    that records the outcome (`Database.set_preview_status`): 'ok' on
    success, 'failed'/'unsupported' with a reason on failure. Video: reuses
    `probe` when the caller already ran FFprobe (e.g. `_probe_and_save`
    probing for VideoDetails), otherwise probes fresh; a failed/missing
    probe means no preview (`False`), recorded as 'failed'. Photo: thumbnail
    + `insert_raw_preview`; an unrecognised format is recorded as
    'unsupported', any other failure as 'failed'. Any other media_type
    (incl. None, non-media files) never has a preview and nothing is
    recorded. Any unexpected exception is recorded as 'failed' with its
    message, then re-raised — callers already isolate per-file failures.
    Always discards `md5_hash` from the "pending previews" registry
    (tasks/preview_registry.py) once this attempt is done, regardless of
    outcome."""
    try:
        if media_type in VIDEO_TYPES:
            if probe is None:
                probe = FFprobe().probe_file(md5_hash=md5_hash, file_path=file_path)
            if probe is None:
                Database().set_preview_status(md5_hash, 'failed', 'FFprobe could not read the file')
                return False
            return create_clip_preview(probe)

        elif media_type in PHOTO_TYPES:
            thumbnail, status, reason = generate_photo_thumbnail(md5_hash, file_path)
            if thumbnail:
                Database().insert_raw_preview(md5_hash, thumbnail, identifier=md5_hash)
                Database().set_preview_status(md5_hash, 'ok')
                return True
            Database().set_preview_status(md5_hash, status, reason)
            return False

        return False
    except Exception as e:
        Database().set_preview_status(md5_hash, 'failed', str(e))
        raise
    finally:
        discard_pending_preview(md5_hash)


def create_clip_preview(input: FFmpegInput) -> bool:
    """Generate+store a video clip preview. Returns whether a preview was
    actually produced — ffmpeg can silently come back with no frames (e.g.
    a near-zero-duration or corrupt file), in which case nothing is stored.
    Used by `_probe_and_save`/`generate_preview` to report preview success
    back to the rescan task (#64), and directly by the DaVinci
    import-metadata path (`scan_files_in_metadata`). Records the outcome via
    `Database.set_preview_status` (#77) so both call sites get it for
    free."""
    result = FFmpeg(input.md5_hash).generate_clip_preview(input)
    if result is not None:
        Database().insert_clip_preview(result, identifier=input.md5_hash)
        Database().set_preview_status(input.md5_hash, 'ok')
        return True
    Database().set_preview_status(input.md5_hash, 'failed', 'ffmpeg produced no frames')
    return False
