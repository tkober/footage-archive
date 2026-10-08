"""Tests for the persistent scan queue (#137, epic #133): db/database.py's
ScanJobs/ScanUnits methods and tasks/scanqueue.py's consumer threads.

The executor is replaced by a fake via monkeypatch for most of these tests
(`monkeypatch.setattr(scanqueue, '_executor', fake)`); a few near the end
use the REAL executor (`api.tracking.run_scan_unit`) with `_probe_and_save`
faked out, same pattern as tests/test_tracking_scan.py, to cover the
crash-safety/idempotent-rerun and path-lock behaviours that only the real
candidate-collection + locking code can exercise.

Every test that starts consumer threads stops them again before returning
(a `finally` plus the autouse `_stop_consumers_after_test` fixture below as
a backstop), and polls for conditions with a bounded timeout instead of
sleeping a fixed amount, so the suite can't hang and shouldn't flake."""

import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from api.tracking import ScanSummary
from db.engine import get_engine
from db.models import files_table
from tasks import scanqueue


def _wait_until(predicate, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), f'condition not met within {timeout}s'


@pytest.fixture(autouse=True)
def _stop_consumers_after_test():
    yield
    scanqueue.stop_consumers(timeout=5)


@pytest.fixture
def fast_poll(monkeypatch):
    """Small poll interval so an idle consumer notices a newly-queued unit
    quickly, without the suite needing real sleeps longer than needed."""
    monkeypatch.setenv('SCAN_QUEUE_POLL_S', '0.05')


# ---------------------------------------------------------------------------
# create_scan_job / dedupe
# ---------------------------------------------------------------------------

def test_create_scan_job_plans_units_in_order_as_planned(db, root_dir):
    dirs = [str(root_dir / f'd{i}') for i in range(3)]
    job, skipped = db.create_scan_job(str(root_dir), [{'directory': d} for d in dirs], {}, start=False)

    assert skipped == []
    assert job['status'] == 'PLANNED'
    assert [u['status'] for u in job['units']] == ['PLANNED', 'PLANNED', 'PLANNED']
    assert [u['directory'] for u in job['units']] == dirs
    assert [u['position'] for u in job['units']] == [0, 1, 2]


def test_create_scan_job_start_true_queues_everything(db, root_dir):
    dirs = [str(root_dir / f'd{i}') for i in range(2)]
    job, skipped = db.create_scan_job(str(root_dir), [{'directory': d} for d in dirs], {}, start=True)

    assert skipped == []
    assert job['status'] == 'QUEUED'
    assert all(u['status'] == 'QUEUED' for u in job['units'])


def test_create_scan_job_dedupes_directory_already_queued_elsewhere(db, root_dir):
    d = str(root_dir / 'shared')
    job1, skipped1 = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)
    assert skipped1 == []

    job2, skipped2 = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)
    assert skipped2 == [{'directory': d, 'reason': 'already queued'}]
    assert job2['units'] == []  # the conflicting insert never created a row at all


def test_create_scan_job_planned_insert_never_conflicts(db, root_dir):
    """A PLANNED unit never trips the partial unique index (QUEUED/RUNNING
    only), even for a directory another job already has QUEUED."""
    d = str(root_dir / 'shared')
    db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)

    job2, skipped2 = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=False)
    assert skipped2 == []
    assert job2['units'][0]['status'] == 'PLANNED'


# ---------------------------------------------------------------------------
# Two consumers, four units — SKIP LOCKED, no double execution
# ---------------------------------------------------------------------------

def test_two_consumers_process_four_units_without_double_execution(db, root_dir, monkeypatch, fast_poll):
    processed: list[str] = []
    lock = threading.Lock()

    def fake(directory, options, report, should_cancel):
        time.sleep(0.05)  # give the other consumer a chance to race
        with lock:
            processed.append(directory)
        return ScanSummary(indexed=1)

    monkeypatch.setattr(scanqueue, '_executor', fake)

    dirs = [str(root_dir / f'd{i}') for i in range(4)]
    job, skipped = db.create_scan_job(str(root_dir), [{'directory': d} for d in dirs], {}, start=True)
    assert skipped == []

    scanqueue.start_consumers(2)
    try:
        _wait_until(lambda: db.get_scan_job(job['id'])['status'] == 'DONE')
    finally:
        scanqueue.stop_consumers()

    assert sorted(processed) == sorted(dirs)
    assert len(processed) == 4

    final = db.get_scan_job(job['id'])
    assert all(u['status'] == 'DONE' for u in final['units'])
    assert 'Indexed 4 files' in final['summary']


# ---------------------------------------------------------------------------
# Pause / resume / cancel / retry
# ---------------------------------------------------------------------------

def test_pause_stops_picking_new_units_resume_continues(db, root_dir, monkeypatch, fast_poll):
    gate = threading.Event()

    def fake(directory, options, report, should_cancel):
        gate.wait(timeout=5)
        return ScanSummary(indexed=1)

    monkeypatch.setattr(scanqueue, '_executor', fake)

    dirs = [str(root_dir / f'd{i}') for i in range(2)]
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d} for d in dirs], {}, start=True)

    scanqueue.start_consumers(1)
    try:
        _wait_until(lambda: db.get_scan_job(job['id'])['units'][0]['status'] == 'RUNNING')

        paused = db.pause_scan_job(job['id'])
        assert paused['status'] == 'PAUSED'

        gate.set()  # let the first (already-claimed) unit finish
        _wait_until(lambda: db.get_scan_job(job['id'])['units'][0]['status'] == 'DONE')

        # Give the consumer a few idle polls — the second unit must stay
        # QUEUED, never claimed, while the job is PAUSED.
        time.sleep(0.3)
        mid = db.get_scan_job(job['id'])
        assert mid['status'] == 'PAUSED'
        assert mid['units'][1]['status'] == 'QUEUED'

        resumed = db.resume_scan_job(job['id'], scanqueue.summarize_job)
        assert resumed['status'] == 'QUEUED'

        _wait_until(lambda: db.get_scan_job(job['id'])['status'] == 'DONE')
    finally:
        gate.set()
        scanqueue.stop_consumers()

    final = db.get_scan_job(job['id'])
    assert all(u['status'] == 'DONE' for u in final['units'])


def test_cancel_running_unit_ends_cancelled_with_partial_counts(db, root_dir, monkeypatch, fast_poll):
    # Counter-based, not wall-clock-based: the loop only stops at
    # should_cancel() or after a huge number of iterations, so a slow/busy
    # test machine can never make it "finish naturally" before the test
    # gets around to cancelling it — it waits for `indexed_some` instead of
    # a fixed sleep.
    indexed_some = threading.Event()

    def fake(directory, options, report, should_cancel):
        indexed = 0
        for _ in range(100_000):
            if should_cancel():
                return ScanSummary(indexed=indexed, cancelled=True)
            indexed += 1
            if indexed == 3:
                indexed_some.set()
            time.sleep(0.005)
        return ScanSummary(indexed=indexed)

    monkeypatch.setattr(scanqueue, '_executor', fake)

    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)

    scanqueue.start_consumers(1)
    try:
        assert indexed_some.wait(timeout=10)
        unit_id = db.get_scan_job(job['id'])['units'][0]['id']

        cancelled_in_db = db.cancel_scan_unit(job['id'], unit_id, scanqueue.summarize_job)
        assert cancelled_in_db is False  # it's RUNNING — caller must flag in-memory
        scanqueue.flag_unit_cancelled(unit_id)

        _wait_until(lambda: db.get_scan_job(job['id'])['units'][0]['status'] == 'CANCELLED')
    finally:
        scanqueue.stop_consumers()

    final = db.get_scan_job(job['id'])
    unit = final['units'][0]
    assert unit['status'] == 'CANCELLED'
    assert unit['result']['indexed'] > 0
    # Only the one unit was cancelled (not the whole job — that's
    # cancel_scan_job, a separate test below), so with no FAILED unit the
    # job still finishes DONE; its summary still counts the cancelled unit.
    assert final['status'] == 'DONE'
    assert final['finished_at'] is not None
    assert '1 cancelled' in final['summary']


def test_pause_lets_running_unit_finish_even_after_db_recheck(db, root_dir, monkeypatch, fast_poll):
    """Pause only stops new claims: a running unit whose should_cancel()
    re-reads the job status from the DB (backstop interval forced to 0
    here) must keep going while the job is PAUSED, not end CANCELLED."""
    monkeypatch.setattr(scanqueue, '_CANCEL_RECHECK_INTERVAL_S', 0.0)
    paused = threading.Event()
    seen_cancel = []

    def fake(directory, options, report, should_cancel):
        paused.wait(timeout=5)
        for _ in range(20):
            seen_cancel.append(should_cancel())
            time.sleep(0.005)
        return ScanSummary(indexed=1)

    monkeypatch.setattr(scanqueue, '_executor', fake)
    job, _ = db.create_scan_job(str(root_dir), [{'directory': str(root_dir / 'd0')}], {}, start=True)

    scanqueue.start_consumers(1)
    try:
        _wait_until(lambda: db.get_scan_job(job['id'])['units'][0]['status'] == 'RUNNING')
        db.pause_scan_job(job['id'])
        paused.set()
        _wait_until(lambda: db.get_scan_job(job['id'])['units'][0]['status'] != 'RUNNING')
    finally:
        paused.set()
        scanqueue.stop_consumers()

    assert not any(seen_cancel)
    final = db.get_scan_job(job['id'])
    assert final['units'][0]['status'] == 'DONE'
    assert final['status'] == 'DONE'


def test_cancel_during_last_batch_ends_cancelled_without_failures(db, root_dir, monkeypatch, fast_poll):
    """Real executor, one batch: the cancel arrives while that (last) batch
    is being probed. The unit must end CANCELLED (not DONE), and the files
    left out because of the cancel must not be reported as failed."""
    import api.tracking as tracking

    unit_dir = root_dir / 'unit'
    unit_dir.mkdir()
    for i in range(6):
        (unit_dir / f'f{i}.jpg').write_bytes(f'content {i}'.encode())

    job, _ = db.create_scan_job(str(root_dir), [{'directory': str(unit_dir)}], {}, start=True)
    unit_id = job['units'][0]['id']
    probed = []

    def probe(sc, db_, generate_clip_preview):
        probed.append(sc.file_name)
        scanqueue.flag_unit_cancelled(unit_id)  # cancel right after the first probe starts

    monkeypatch.setattr(tracking, '_probe_and_save', probe)
    monkeypatch.setenv('SCAN_BATCH_SIZE', '100')

    scanqueue.start_consumers(1)
    try:
        _wait_until(lambda: db.get_scan_job(job['id'])['units'][0]['status'] not in ('QUEUED', 'RUNNING'))
    finally:
        scanqueue.stop_consumers()

    unit = db.get_scan_job(job['id'])['units'][0]
    assert unit['status'] == 'CANCELLED'
    assert unit['result']['failed'] == 0
    assert len(probed) < 6


def test_cancel_queued_and_planned_units_immediately(db, root_dir):
    dirs = [str(root_dir / f'd{i}') for i in range(2)]
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d} for d in dirs], {}, start=True)

    cancelled = db.cancel_scan_job(job['id'], scanqueue.summarize_job)
    assert cancelled['status'] == 'CANCELLED'
    assert all(u['status'] == 'CANCELLED' for u in cancelled['units'])
    assert cancelled['finished_at'] is not None


def test_retry_failed_unit_and_job_status_transitions(db, root_dir, monkeypatch, fast_poll):
    def fake_fail(directory, options, report, should_cancel):
        raise RuntimeError('boom')

    monkeypatch.setattr(scanqueue, '_executor', fake_fail)

    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)

    scanqueue.start_consumers(1)
    try:
        _wait_until(lambda: db.get_scan_job(job['id'])['status'] == 'FAILED')
    finally:
        scanqueue.stop_consumers()

    failed = db.get_scan_job(job['id'])
    assert failed['units'][0]['status'] == 'FAILED'
    assert failed['units'][0]['error'] == 'boom'
    assert failed['finished_at'] is not None
    unit_id = failed['units'][0]['id']

    def fake_ok(directory, options, report, should_cancel):
        return ScanSummary(indexed=5)

    monkeypatch.setattr(scanqueue, '_executor', fake_ok)

    retried = db.retry_scan_unit(job['id'], unit_id, scanqueue.summarize_job)
    assert retried['status'] == 'QUEUED'
    assert retried['finished_at'] is None
    assert retried['units'][0]['status'] == 'QUEUED'
    assert retried['units'][0]['error'] is None

    scanqueue.start_consumers(1)
    try:
        _wait_until(lambda: db.get_scan_job(job['id'])['status'] == 'DONE')
    finally:
        scanqueue.stop_consumers()

    done = db.get_scan_job(job['id'])
    assert done['units'][0]['result']['indexed'] == 5


def test_retry_rejects_unit_not_failed_or_cancelled(db, root_dir):
    from db.database import InvalidScanTransitionError

    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)
    unit_id = job['units'][0]['id']

    with pytest.raises(InvalidScanTransitionError):
        db.retry_scan_unit(job['id'], unit_id, scanqueue.summarize_job)  # still QUEUED


def test_retry_directory_already_queued_elsewhere_raises(db, root_dir):
    from db.database import DirectoryAlreadyQueuedError

    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)
    unit_id = job['units'][0]['id']
    db.cancel_scan_job(job['id'], scanqueue.summarize_job)  # unit -> CANCELLED

    # Someone else queues the same directory now.
    db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)

    with pytest.raises(DirectoryAlreadyQueuedError):
        db.retry_scan_unit(job['id'], unit_id, scanqueue.summarize_job)


def test_move_unit_to_top_reorders_waiting_units(db, root_dir):
    dirs = [str(root_dir / f'd{i}') for i in range(3)]
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d} for d in dirs], {}, start=True)
    last_unit = job['units'][2]

    moved = db.move_scan_unit_to_top(job['id'], last_unit['id'])
    assert [u['id'] for u in moved['units']] == [last_unit['id'], job['units'][0]['id'], job['units'][1]['id']]


def test_deselect_and_reselect_only_for_planned(db, root_dir):
    from db.database import InvalidScanTransitionError

    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=False)
    unit_id = job['units'][0]['id']

    deselected = db.deselect_scan_unit(job['id'], unit_id)
    assert deselected['units'][0]['status'] == 'DESELECTED'

    with pytest.raises(InvalidScanTransitionError):
        db.deselect_scan_unit(job['id'], unit_id)  # already DESELECTED

    reselected = db.reselect_scan_unit(job['id'], unit_id)
    assert reselected['units'][0]['status'] == 'PLANNED'


def test_start_scan_job_queues_planned_units_and_cancels_duplicates(db, root_dir):
    d = str(root_dir / 'contested')
    other_job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)

    planned_job, _ = db.create_scan_job(
        str(root_dir), [{'directory': d}, {'directory': str(root_dir / 'free')}], {}, start=False,
    )
    started = db.start_scan_job(planned_job['id'])

    assert started['status'] == 'QUEUED'
    contested_unit = next(u for u in started['units'] if u['directory'] == d)
    free_unit = next(u for u in started['units'] if u['directory'] == str(root_dir / 'free'))
    assert contested_unit['status'] == 'CANCELLED'
    assert contested_unit['error'] == 'Already queued in another job'
    assert free_unit['status'] == 'QUEUED'


def test_delete_scan_job_rejects_while_unit_running(db, root_dir, monkeypatch, fast_poll):
    from db.database import InvalidScanTransitionError

    gate = threading.Event()

    def fake(directory, options, report, should_cancel):
        gate.wait(timeout=5)
        return ScanSummary(indexed=1)

    monkeypatch.setattr(scanqueue, '_executor', fake)

    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)

    scanqueue.start_consumers(1)
    try:
        _wait_until(lambda: db.get_scan_job(job['id'])['units'][0]['status'] == 'RUNNING')
        with pytest.raises(InvalidScanTransitionError):
            db.delete_scan_job(job['id'])
        gate.set()
        _wait_until(lambda: db.get_scan_job(job['id'])['status'] == 'DONE')
    finally:
        gate.set()
        scanqueue.stop_consumers()

    db.delete_scan_job(job['id'])  # now fine
    assert db.get_scan_job(job['id']) is None


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------

def test_recover_scan_queue_resets_running_units_and_jobs(db, root_dir):
    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)

    claim = db.claim_next_scan_unit()
    assert claim is not None
    running = db.get_scan_job(job['id'])
    assert running['status'] == 'RUNNING'
    assert running['units'][0]['status'] == 'RUNNING'

    db.recover_scan_queue(scanqueue.summarize_job)

    recovered = db.get_scan_job(job['id'])
    assert recovered['status'] == 'QUEUED'
    assert recovered['units'][0]['status'] == 'QUEUED'
    assert recovered['units'][0]['started_at'] is None


def test_recover_scan_queue_cancels_stray_units_of_cancelled_job(db, root_dir):
    """A unit left RUNNING when a job-level cancel had already been issued
    (process restarted before the running unit noticed its in-memory
    cancel flag) is reset to QUEUED by the step above, then — since its job
    is CANCELLED — this reconciles it to CANCELLED too, and the job finally
    gets its finished_at/summary (see Database.recover_scan_queue)."""
    dirs = [str(root_dir / f'd{i}') for i in range(2)]
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d} for d in dirs], {}, start=True)
    claim = db.claim_next_scan_unit()  # one unit RUNNING, one still QUEUED
    assert claim is not None

    db.cancel_scan_job(job['id'], scanqueue.summarize_job)
    mid = db.get_scan_job(job['id'])
    assert mid['status'] == 'CANCELLED'
    assert mid['finished_at'] is None  # the RUNNING unit is still open

    db.recover_scan_queue(scanqueue.summarize_job)

    recovered = db.get_scan_job(job['id'])
    assert recovered['status'] == 'CANCELLED'
    assert all(u['status'] == 'CANCELLED' for u in recovered['units'])
    assert recovered['finished_at'] is not None


def test_simulated_restart_reruns_running_unit_with_real_executor(db, root_dir, monkeypatch, fast_poll):
    """Simulated restart: a unit left RUNNING (e.g. the process crashed
    mid-scan) is reset to QUEUED by recover_scan_queue() and re-executed by
    a consumer with the REAL executor — thanks to #136's crash-safety rule
    (no signature written until a probe finishes), the rerun is a clean,
    idempotent re-index: exactly one Files row for the one file, same as a
    single untouched run would produce."""
    import api.tracking as tracking
    monkeypatch.setattr(tracking, '_probe_and_save', lambda *a, **k: None)

    unit_dir = root_dir / 'unit'
    unit_dir.mkdir()
    (unit_dir / 'a.jpg').write_bytes(b'hello world')

    job, _ = db.create_scan_job(str(root_dir), [{'directory': str(unit_dir)}], {}, start=True)

    claim = db.claim_next_scan_unit()  # simulate: a consumer claimed it, then the process died
    assert claim is not None

    db.recover_scan_queue(scanqueue.summarize_job)
    assert db.get_scan_job(job['id'])['units'][0]['status'] == 'QUEUED'

    scanqueue.start_consumers(1)
    try:
        _wait_until(lambda: db.get_scan_job(job['id'])['status'] == 'DONE')
    finally:
        scanqueue.stop_consumers()

    with get_engine().connect() as conn:
        rows = conn.execute(select(files_table)).fetchall()
    assert len(rows) == 1
    assert rows[0].file_name == 'a.jpg'
    assert rows[0].directory == str(unit_dir)


# ---------------------------------------------------------------------------
# Real executor: direct files only (no recursion) + path-lock interaction
# ---------------------------------------------------------------------------

def test_real_executor_tracks_only_direct_files_and_blocks_parent_not_sibling(
    db, root_dir, monkeypatch, fast_poll,
):
    import api.tracking as tracking
    import fileops.service as fileops_service
    from fileops.pathlocks import PathLockedError

    parent = root_dir / 'parent'
    unit_dir = parent / 'unit'
    unit_dir.mkdir(parents=True)
    (unit_dir / 'a.jpg').write_bytes(b'x')
    nested = unit_dir / 'nested'
    nested.mkdir()
    (nested / 'b.jpg').write_bytes(b'y')  # must stay untracked — no recursion

    sibling = parent / 'sibling'
    sibling.mkdir()

    probe_started = threading.Event()
    release_probe = threading.Event()

    def blocking_probe(sc, db_, generate_clip_preview):
        probe_started.set()
        release_probe.wait(timeout=5)

    monkeypatch.setattr(tracking, '_probe_and_save', blocking_probe)

    job, _ = db.create_scan_job(str(root_dir), [{'directory': str(unit_dir)}], {}, start=True)

    scanqueue.start_consumers(1)
    try:
        assert probe_started.wait(timeout=5)

        with pytest.raises(PathLockedError):
            fileops_service.rename_path(str(parent), 'parent-renamed')

        new_sibling_path = fileops_service.rename_path(str(sibling), 'sibling-renamed')
        assert Path(new_sibling_path).name == 'sibling-renamed'

        release_probe.set()
        _wait_until(lambda: db.get_scan_job(job['id'])['status'] == 'DONE')
    finally:
        release_probe.set()
        scanqueue.stop_consumers()

    with get_engine().connect() as conn:
        rows = conn.execute(select(files_table.c.file_name)).fetchall()
    assert {r.file_name for r in rows} == {'a.jpg'}
