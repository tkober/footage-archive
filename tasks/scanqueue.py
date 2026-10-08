"""Persistent scan queue consumers (#137, epic #133).

Directory scans no longer run as FastAPI `BackgroundTasks` (those share
anyio's threadpool with the sync endpoints — rename/move/EXIF/stream — so a
waiting scan would block one of them). Instead a scan is a `ScanJobs` row
with one `ScanUnits` row per directory (no recursion — planning a whole tree
into many units is #138), both in Postgres (`db/database.py`'s "Scan queue"
section), worked off by a handful of daemon consumer threads started here.
`Rediscover`, `Rescan`, `Track file` and `Import metadata` are unaffected —
they still go through `tasks/taskmanager.py`.

The unit executor itself is `api/tracking.py::run_scan_unit` — reached only
through `_get_executor()` below (a module attribute, swappable by tests via
`monkeypatch.setattr(scanqueue, '_executor', fake)`) and imported lazily so
this module never pulls in api/tracking.py (and everything it imports) at
process startup just to start the consumers."""

import logging
import threading
import time
from dataclasses import asdict
from typing import Callable, Optional

from db.database import Database
from env.environment import Environment
from tasks import activity
from tasks.activity import TaskActivity

logger = logging.getLogger(__name__)

# Injectable executor hook — tests replace this directly; production code
# never writes it, so _get_executor()'s lazy import runs exactly once.
_executor: Optional[Callable] = None

_stop_event = threading.Event()
_threads: list[threading.Thread] = []
_state_lock = threading.Lock()
# unit_id -> job_id, for every unit THIS process currently has claimed —
# lets a job-level cancel find which of its units are RUNNING right now.
_running_units: dict[int, str] = {}
# unit ids flagged cancelled (in-memory, same-process only) — should_cancel()
# checks this before anything else; a restart loses it, which is fine (see
# Database.recover_scan_queue's docstring for why that's still correct).
_cancelled_unit_ids: set[int] = set()
# unit_id -> TaskActivity, for RUNNING units claimed by this process — read
# by api/tasks.py / api/scanjobs.py to report a unit's live activity.
_activities: dict[int, TaskActivity] = {}

# How often (seconds) a long-running unit re-checks its job's status in the
# DB as a backstop to the in-memory cancel flag — covers a cancel whose
# in-memory flag_* call this consumer thread somehow missed; it does NOT
# need to (and cannot) survive a process restart, see recover_scan_queue().
_CANCEL_RECHECK_INTERVAL_S = 3.0
# At most one progress DB write per second per unit; the *last* message is
# always flushed once more after the executor returns (see _run_unit).
_PROGRESS_THROTTLE_S = 1.0


def _get_executor() -> Callable:
    global _executor
    if _executor is None:
        from api.tracking import run_scan_unit
        _executor = run_scan_unit
    return _executor


def summarize_job(units: list[dict]) -> str:
    """Builds a job's final summary text from its units' terminal
    `status`/`result` (#137) — `result` is None for a unit that FAILED
    outside the file loop (lock/DB/directory-vanished) and so contributes
    no counts, same as a unit with an empty result. Reused by every
    db/database.py transition method that can finalize a job (passed in as
    `summarize`, never imported there — see that module's own note on why)."""
    from api.tracking import ScanSummary, _format_scan_summary

    total = ScanSummary()
    folders_failed = 0
    cancelled = 0
    for unit in units:
        if unit['status'] == 'FAILED':
            folders_failed += 1
        elif unit['status'] == 'CANCELLED':
            cancelled += 1
        result = unit.get('result') or {}
        total = total + ScanSummary(
            indexed=result.get('indexed', 0), relinked=result.get('relinked', 0),
            conflicts=result.get('conflicts', 0), failed=result.get('failed', 0),
            skipped=result.get('skipped', 0),
        )
    message = _format_scan_summary(total)
    if folders_failed:
        message += f' · {folders_failed} folder{"" if folders_failed == 1 else "s"} failed'
    if cancelled:
        message += f' · {cancelled} cancelled'
    return message


def get_activity(unit_id: int):
    """Live `Activity` of a RUNNING unit this process claimed, or None (unit
    not claimed here — e.g. a different process in a future multi-process
    deployment, or simply not RUNNING). Mirrors
    `tasks.taskmanager.TaskManager.get_activity`."""
    task_activity = _activities.get(unit_id)
    if task_activity is None:
        return None
    from tasks.loadcontrol import is_throttled
    return task_activity.snapshot(throttled=is_throttled())


def flag_job_cancelled(job_id: str) -> None:
    """Flags every unit this process currently has RUNNING for `job_id` as
    cancelled in memory — call right after a job/unit cancel is written to
    the DB (api/scanjobs.py), so a running unit's `should_cancel()` notices
    on its very next per-file check instead of waiting for the periodic DB
    re-check."""
    with _state_lock:
        for unit_id, running_job_id in list(_running_units.items()):
            if running_job_id == job_id:
                _cancelled_unit_ids.add(unit_id)


def flag_unit_cancelled(unit_id: int) -> None:
    with _state_lock:
        _cancelled_unit_ids.add(unit_id)


def _make_should_cancel(unit_id: int, job_id: str) -> Callable[[], bool]:
    last_check = [0.0]

    def should_cancel() -> bool:
        with _state_lock:
            if unit_id in _cancelled_unit_ids:
                return True
        now = time.monotonic()
        if now - last_check[0] < _CANCEL_RECHECK_INTERVAL_S:
            return False
        last_check[0] = now
        try:
            status = Database().get_scan_job_status(job_id)
        except Exception:
            logger.exception('Scan unit %s: failed to re-check its job status', unit_id)
            return False
        # Only a cancelled (or vanished) job stops a running unit. A PAUSED
        # job lets its running units finish — pause only stops new claims.
        if status is None or status == 'CANCELLED':
            flag_unit_cancelled(unit_id)
            return True
        return False

    return should_cancel


def _make_report(unit_id: int) -> tuple[Callable[[str], None], Callable[[], None]]:
    """Throttled `report` callback + a `flush_last` the caller invokes once
    the executor returns, so the very last progress message always lands in
    the DB even if the throttle would otherwise have dropped it."""
    last_write = [0.0]
    last_message = ['']

    def report(message: str) -> None:
        last_message[0] = message
        now = time.monotonic()
        if now - last_write[0] < _PROGRESS_THROTTLE_S:
            return
        last_write[0] = now
        try:
            Database().set_scan_unit_progress(unit_id, message)
        except Exception:
            logger.exception('Failed to write progress for scan unit %s', unit_id)

    def flush_last() -> None:
        if not last_message[0]:
            return
        try:
            Database().set_scan_unit_progress(unit_id, last_message[0])
        except Exception:
            logger.exception('Failed to flush final progress for scan unit %s', unit_id)

    return report, flush_last


def _run_unit(claim: dict) -> None:
    unit_id = claim['unit_id']
    job_id = claim['job_id']
    directory = claim['directory']
    options = claim['options'] or {}

    task_activity = TaskActivity()
    with _state_lock:
        _running_units[unit_id] = job_id
        _cancelled_unit_ids.discard(unit_id)
        _activities[unit_id] = task_activity

    report, flush_last = _make_report(unit_id)
    should_cancel = _make_should_cancel(unit_id, job_id)
    db = Database()

    try:
        with activity.bound(task_activity):
            summary = _get_executor()(directory, options, report, should_cancel)
        flush_last()
        result = asdict(summary)
        cancelled = result.pop('cancelled', False)
        db.finish_scan_unit(unit_id, 'CANCELLED' if cancelled else 'DONE',
                            result=result, error=None, summarize=summarize_job)
    except Exception as e:
        flush_last()
        logger.exception('Scan unit %s (%s) failed', unit_id, directory)
        try:
            db.finish_scan_unit(unit_id, 'FAILED', result=None, error=str(e), summarize=summarize_job)
        except Exception:
            logger.exception('Failed to mark scan unit %s FAILED after an error', unit_id)
    finally:
        with _state_lock:
            _running_units.pop(unit_id, None)
            _activities.pop(unit_id, None)
            _cancelled_unit_ids.discard(unit_id)


def _consumer_loop(index: int) -> None:
    db = Database()
    poll_s = Environment().get_scan_queue_poll_s()
    while not _stop_event.is_set():
        try:
            claim = db.claim_next_scan_unit()
        except Exception:
            logger.exception('Scan queue consumer %s failed to claim a unit — backing off', index)
            _stop_event.wait(poll_s)
            continue
        if claim is None:
            _stop_event.wait(poll_s)
            continue
        try:
            _run_unit(claim)
        except Exception:
            # _run_unit already isolates the executor itself; this only
            # guards against a bug in _run_unit's own bookkeeping — the
            # consumer must survive it and keep polling.
            logger.exception('Scan queue consumer %s crashed running unit %s',
                            index, claim.get('unit_id'))


def start_consumers(n: Optional[int] = None) -> None:
    """Starts `n` (default: `SCAN_CONSUMERS` env, default 2) daemon consumer
    threads. No-op if consumers are already running — call stop_consumers()
    first to restart with a different count (tests do this routinely)."""
    global _threads
    if _threads:
        return
    _stop_event.clear()
    count = n if n is not None else Environment().get_scan_consumers()
    _threads = [
        threading.Thread(target=_consumer_loop, args=(i,), name=f'scan-consumer-{i}', daemon=True)
        for i in range(count)
    ]
    for t in _threads:
        t.start()


def stop_consumers(timeout: float = 5.0) -> None:
    """Signals every consumer thread to stop (the poll sleep waits on the
    stop event, so this is prompt) and joins them, each bounded by
    `timeout`. Safe to call when nothing is running. Every test that starts
    consumers must call this in its teardown so no thread outlives the
    test."""
    global _threads
    _stop_event.set()
    for t in _threads:
        t.join(timeout=timeout)
    _threads = []
    _stop_event.clear()
    with _state_lock:
        _running_units.clear()
        _activities.clear()
        _cancelled_unit_ids.clear()


def recover_on_startup() -> None:
    """Run once at app startup, before consumers start (#137) — see
    `Database.recover_scan_queue`'s own docstring for exactly what this
    reconciles and why."""
    Database().recover_scan_queue(summarize_job)
