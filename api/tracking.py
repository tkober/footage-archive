import logging
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Callable

import pandas as pd
from fastapi import APIRouter, HTTPException, BackgroundTasks, Response

from api.dtos import (
    ConflictCandidate,
    ConflictCountResponse,
    ConflictEntry,
    FileQuery,
    RediscoverQuery,
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
from ffmpeg.ffmpeg import FFmpegInput, FFmpeg, FFprobe
from photos.exif import probe_photo, generate_photo_thumbnail
from scanner.scanner import Scanner, ScanResult
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


@TrackingApi.post('/import-metadata')
async def import_metadata(query: FileQuery, background_tasks: BackgroundTasks):
    path = Path(query.path)
    if path.is_dir():
        raise HTTPException(status_code=400, detail='Provided path is a directory')

    if not path.exists():
        raise HTTPException(status_code=404, detail='File not found')

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
    running tally is guarded by a lock and reported as 'done / total'."""

    def __init__(self, total: int, report: Callable[[str], None]):
        self._total = total
        self._report = report
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
            self._report(f'Probed {self._done} / {self._total}{suffix}: {file_name}')


def index_files_in_directory(query: FileQuery, report: Callable[[str], None]):
    directory = Path(query.path)
    with shared(str(directory)):
        report('Scanning files…')
        scan_results = Scanner().scan_directory(directory)
        report(f'Found {len(scan_results)} files, reconciling…')
        db = Database()
        _scan_and_reconcile(scan_results, db, report,
                            generate_clip_preview=query.generate_clip_preview,
                            scanned_directory=str(directory))


def rediscover_directory(query: RediscoverQuery, report: Callable[[str], None]):
    directory = Path(query.path)
    with shared(str(directory)):
        report('Hashing files…')
        scan_results = Scanner().scan_directory(directory)
        total = len(scan_results)
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

            def probe(sc: ScanResult):
                ok = True
                try:
                    _probe_and_save(sc, db, generate_clip_preview=query.generate_clip_preview)
                except Exception:
                    logging.exception(f'Failed to probe {sc.directory}/{sc.file_name}')
                    ok = False
                finally:
                    progress.record(sc.file_name, ok)

            parallel_map(to_track, probe)

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
                        generate_clip_preview: bool, scanned_directory: str) -> None:
    """Shared reconciliation path for the normal scan (directory + single
    file): classify every hash found against the DB using the same rules as
    `/tracking/rediscover` (fileops/rediscover.py), then probe every hash
    that ends up at a settled tracked path — new (first sorted path wins,
    other copies become conflicts), unchanged (re-probed to bump
    last_indexed_at), or relinked (tracked path was gone, found exactly
    once, so it was relinked like a rediscover before probing at the new
    path). A hash left in conflict (old path still exists, or found more
    than once with the old path gone) is never inserted/probed and its
    `Files` row is left untouched."""
    md5_hashes = sorted({sc.md5_hash for sc in scan_results})
    tracked = db.get_tracked_paths_for_hashes(md5_hashes)
    report('Matching against database…')
    classification = classify_rediscover(scan_results, tracked, exists=lambda p: Path(p).exists())

    scan_results_by_path = {f'{sc.directory}/{sc.file_name}': sc for sc in scan_results}

    # One shared progress counter across both probing phases below (new
    # hashes tracked during apply_rediscover(), then unchanged/relinked
    # hashes probed at their settled path afterwards) so the per-file
    # "Probed X / Y" messages stay monotonic for the whole scan.
    total_to_probe = len(classification.new) + len(classification.unchanged) + len(classification.relinked)
    progress = _ProbeProgress(total_to_probe, report)

    def probe_one(sc: ScanResult):
        ok = True
        try:
            _probe_and_save(sc, db, generate_clip_preview=generate_clip_preview)
        except Exception:
            # Isolate per-file failures so one bad file doesn't abort the whole scan.
            logging.exception(f'Failed to probe {sc.directory}/{sc.file_name}')
            ok = False
        finally:
            progress.record(sc.file_name, ok)

    def probe_batch(batch: list[ScanResult]):
        if not batch:
            return
        # `insert_scan_results` is only ever called here with ScanResults that
        # already match the (post-relink) tracked path, so this upsert can
        # never move a Files row — it only inserts new hashes or bumps
        # last_indexed_at for ones that stayed put.
        db.insert_scan_results(batch)
        parallel_map(batch, probe_one)

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
    report(f'Indexed {indexed} files · {result.relinked} relinked · {result.conflicts} conflicts')


def index_single_file(query: FileQuery, report: Callable[[str], None]):
    path = Path(query.path)
    with shared(str(path)):
        report('Hashing file…')
        scan_results = Scanner().scan_files([path])
        if not scan_results:
            return
        db = Database()
        _scan_and_reconcile(scan_results, db, report,
                            generate_clip_preview=query.generate_clip_preview,
                            scanned_directory=str(path.parent))


def _probe_and_save(sc: ScanResult, db: Database, generate_clip_preview: bool):
    file_path = sc.directory + '/' + sc.file_name
    last_modified_at = datetime.fromtimestamp(Path(file_path).stat().st_mtime).isoformat()

    if sc.media_type in VIDEO_TYPES:
        probe = FFprobe().probe_file(md5_hash=sc.md5_hash, file_path=file_path)
        if probe is None:
            logging.warning(f'FFprobe failed for {file_path}')
            _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=None)
            return

        _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=probe.recorded_at)
        db.insert_video_details(pd.DataFrame([probe.model_dump()]))

        if generate_clip_preview:
            create_clip_preview(probe)

    elif sc.media_type in PHOTO_TYPES:
        probe = probe_photo(md5_hash=sc.md5_hash, file_path=file_path)
        if probe is None:
            _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=None)
        else:
            _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=probe.recorded_at,
                               latitude=probe.latitude, longitude=probe.longitude,
                               altitude=probe.altitude)
            db.insert_photo_details(pd.DataFrame([probe.model_dump()]))

        if generate_clip_preview:
            thumbnail = generate_photo_thumbnail(sc.md5_hash, file_path)
            if thumbnail:
                db.insert_raw_preview(sc.md5_hash, thumbnail, identifier=sc.md5_hash)

    else:
        _save_file_details(db, sc.md5_hash, last_modified_at, recorded_at=None)


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


def create_clip_preview(input: FFmpegInput):
    result = FFmpeg(input.md5_hash).generate_clip_preview(input)
    if result is not None:
        Database().insert_clip_preview(result, identifier=input.md5_hash)
