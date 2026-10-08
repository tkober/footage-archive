"""HTTP-level tests for the scan queue's API (#137): api/scanjobs.py's
`/scan-jobs` endpoints, and the synthetic entries api/tasks.py's `/tasks`
gains for any job that isn't PLANNED. Pattern follows
tests/test_conflicts_api.py — a throwaway FastAPI app with just the
router(s) under test, TestClient, the shared `db`/`root_dir` fixtures."""

import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.scanjobs import ScanJobsApi
from api.tasks import TasksApi
from api.tracking import ScanSummary
from tasks import scanqueue


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(ScanJobsApi)
    app.include_router(TasksApi)
    return app, TestClient(app)


def _wait_until(predicate, timeout=5.0, interval=0.02):
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
    monkeypatch.setenv('SCAN_QUEUE_POLL_S', '0.05')


# ---------------------------------------------------------------------------
# OpenAPI schema
# ---------------------------------------------------------------------------

def test_scan_jobs_routes_appear_in_openapi_schema():
    app, _ = _make_client()
    paths = app.openapi()['paths']
    assert '/scan-jobs' in paths
    assert '/scan-jobs/{job_id}' in paths
    assert '/scan-jobs/{job_id}/start' in paths
    assert '/scan-jobs/{job_id}/pause' in paths
    assert '/scan-jobs/{job_id}/resume' in paths
    assert '/scan-jobs/{job_id}/cancel' in paths
    assert '/scan-jobs/{job_id}/units/{unit_id}/cancel' in paths
    assert '/scan-jobs/{job_id}/units/{unit_id}/retry' in paths
    assert '/scan-jobs/{job_id}/units/{unit_id}/move-top' in paths
    assert '/scan-jobs/{job_id}/units/{unit_id}/deselect' in paths
    assert '/scan-jobs/{job_id}/units/{unit_id}/reselect' in paths


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_get_scan_jobs_and_detail(db, root_dir):
    _, client = _make_client()
    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d, 'media_file_count': 3}], {}, start=True)

    listed = client.get('/scan-jobs').json()
    assert any(j['id'] == job['id'] for j in listed)
    entry = next(j for j in listed if j['id'] == job['id'])
    assert entry['units_total'] == 1
    assert entry['files_total'] == 3
    assert entry['units_done'] == 0

    detail = client.get(f"/scan-jobs/{job['id']}").json()
    assert detail['id'] == job['id']
    assert len(detail['units']) == 1
    assert detail['units'][0]['directory'] == d


def test_get_scan_jobs_active_filter(db, root_dir):
    _, client = _make_client()
    d1 = str(root_dir / 'a')
    d2 = str(root_dir / 'b')
    job_done, _ = db.create_scan_job(str(root_dir), [{'directory': d1}], {}, start=True)
    db.cancel_scan_job(job_done['id'], scanqueue.summarize_job)
    job_active, _ = db.create_scan_job(str(root_dir), [{'directory': d2}], {}, start=True)

    active = client.get('/scan-jobs', params={'active': True}).json()
    ids = {j['id'] for j in active}
    assert job_active['id'] in ids
    assert job_done['id'] not in ids

    everything = client.get('/scan-jobs').json()
    ids_all = {j['id'] for j in everything}
    assert {job_active['id'], job_done['id']} <= ids_all


def test_pause_resume_cancel_endpoints(db, root_dir):
    _, client = _make_client()
    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)

    paused = client.post(f"/scan-jobs/{job['id']}/pause")
    assert paused.status_code == 200
    assert paused.json()['status'] == 'PAUSED'

    resumed = client.post(f"/scan-jobs/{job['id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()['status'] == 'QUEUED'

    cancelled = client.post(f"/scan-jobs/{job['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()['status'] == 'CANCELLED'


def test_unit_retry_move_top_deselect_reselect_endpoints(db, root_dir):
    _, client = _make_client()
    dirs = [str(root_dir / f'd{i}') for i in range(2)]
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d} for d in dirs], {}, start=False)
    unit_id = job['units'][1]['id']

    deselected = client.post(f"/scan-jobs/{job['id']}/units/{unit_id}/deselect")
    assert deselected.status_code == 200
    assert deselected.json()['units'][1]['status'] == 'DESELECTED'

    reselected = client.post(f"/scan-jobs/{job['id']}/units/{unit_id}/reselect")
    assert reselected.status_code == 200
    assert reselected.json()['units'][1]['status'] == 'PLANNED'

    moved = client.post(f"/scan-jobs/{job['id']}/units/{unit_id}/move-top")
    assert moved.status_code == 200
    assert moved.json()['units'][0]['id'] == unit_id

    start_resp = client.post(f"/scan-jobs/{job['id']}/start")
    assert start_resp.status_code == 200
    db.cancel_scan_job(job['id'], scanqueue.summarize_job)

    retried = client.post(f"/scan-jobs/{job['id']}/units/{unit_id}/retry")
    assert retried.status_code == 200
    assert retried.json()['status'] == 'QUEUED'


def test_delete_scan_job(db, root_dir):
    _, client = _make_client()
    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)
    db.cancel_scan_job(job['id'], scanqueue.summarize_job)

    resp = client.delete(f"/scan-jobs/{job['id']}")
    assert resp.status_code == 204
    assert client.get(f"/scan-jobs/{job['id']}").status_code == 404


# ---------------------------------------------------------------------------
# 404 / 409
# ---------------------------------------------------------------------------

def test_unknown_job_id_is_404(db, root_dir):
    _, client = _make_client()
    assert client.get('/scan-jobs/does-not-exist').status_code == 404
    assert client.post('/scan-jobs/does-not-exist/pause').status_code == 404
    assert client.post('/scan-jobs/does-not-exist/resume').status_code == 404
    assert client.post('/scan-jobs/does-not-exist/cancel').status_code == 404
    assert client.delete('/scan-jobs/does-not-exist').status_code == 404


def test_unknown_unit_id_is_404(db, root_dir):
    _, client = _make_client()
    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)

    assert client.post(f"/scan-jobs/{job['id']}/units/999999/cancel").status_code == 404
    assert client.post(f"/scan-jobs/{job['id']}/units/999999/retry").status_code == 404
    assert client.post(f"/scan-jobs/{job['id']}/units/999999/move-top").status_code == 404


def test_invalid_transitions_are_409(db, root_dir):
    _, client = _make_client()
    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)

    # Can't resume a job that isn't PAUSED.
    assert client.post(f"/scan-jobs/{job['id']}/resume").status_code == 409
    # Can't start a job that isn't PLANNED.
    assert client.post(f"/scan-jobs/{job['id']}/start").status_code == 409

    unit_id = job['units'][0]['id']
    # Can't retry a unit that's still QUEUED.
    assert client.post(f"/scan-jobs/{job['id']}/units/{unit_id}/retry").status_code == 409

    db.cancel_scan_job(job['id'], scanqueue.summarize_job)
    # Can't cancel an already-CANCELLED job.
    assert client.post(f"/scan-jobs/{job['id']}/cancel").status_code == 409
    # Can't delete... actually deleting a cancelled job is fine; check pause instead.
    assert client.post(f"/scan-jobs/{job['id']}/pause").status_code == 409


# ---------------------------------------------------------------------------
# GET /tasks transition
# ---------------------------------------------------------------------------

def test_tasks_listing_shows_synthetic_entry_for_non_planned_job(db, root_dir):
    _, client = _make_client()
    d = str(root_dir / 'a')
    planned_job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=False)
    queued_job, _ = db.create_scan_job(str(root_dir), [{'directory': str(root_dir / 'b')}], {}, start=True)

    tasks = client.get('/tasks/').json()
    ids = {t['id'] for t in tasks}
    assert planned_job['id'] not in ids  # PLANNED jobs are never shown
    assert queued_job['id'] in ids

    entry = next(t for t in tasks if t['id'] == queued_job['id'])
    assert entry['name'] == 'Scan directory'
    assert entry['description'] == f'Scanning directory "{root_dir}".'
    assert entry['status'] == 'QUEUED'
    assert '0 / 1 folders' in entry['progress']

    # GET /tasks/{id} works for a job id too.
    single = client.get(f"/tasks/{queued_job['id']}")
    assert single.status_code == 200
    assert single.json()['id'] == queued_job['id']

    assert client.get(f"/tasks/{planned_job['id']}").status_code == 404


def test_tasks_entry_reports_done_with_summary_and_conflicts_parseable(db, root_dir, monkeypatch, fast_poll):
    monkeypatch.setattr(scanqueue, '_executor', lambda directory, options, report, should_cancel:
                        ScanSummary(indexed=2, conflicts=1))

    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)

    _, client = _make_client()
    scanqueue.start_consumers(1)
    try:
        _wait_until(lambda: db.get_scan_job(job['id'])['status'] == 'DONE')
    finally:
        scanqueue.stop_consumers()

    entry = next(t for t in client.get('/tasks/').json() if t['id'] == job['id'])
    assert entry['status'] == 'COMPLETED'
    # Same regex the tasks-widget frontend parses conflicts out of
    # (`/(\d+)\s+conflicts?/`) — the job summary must match it too.
    import re
    match = re.search(r'(\d+)\s+conflicts?', entry['progress'])
    assert match is not None
    assert match.group(1) == '1'


def test_delete_completed_task_endpoint_removes_finished_job(db, root_dir):
    d = str(root_dir / 'a')
    job, _ = db.create_scan_job(str(root_dir), [{'directory': d}], {}, start=True)
    db.cancel_scan_job(job['id'], scanqueue.summarize_job)

    _, client = _make_client()
    resp = client.delete(f"/tasks/{job['id']}")
    assert resp.status_code == 200
    assert resp.json()['id'] == job['id']
    assert db.get_scan_job(job['id']) is None


def test_clear_completed_tasks_removes_done_and_cancelled_jobs_not_failed(db, root_dir, monkeypatch, fast_poll):
    monkeypatch.setattr(scanqueue, '_executor', lambda directory, options, report, should_cancel:
                        (_ for _ in ()).throw(RuntimeError('boom')))

    failed_dir = str(root_dir / 'failing')
    failed_job, _ = db.create_scan_job(str(root_dir), [{'directory': failed_dir}], {}, start=True)

    scanqueue.start_consumers(1)
    try:
        _wait_until(lambda: db.get_scan_job(failed_job['id'])['status'] == 'FAILED')
    finally:
        scanqueue.stop_consumers()

    cancelled_job, _ = db.create_scan_job(str(root_dir), [{'directory': str(root_dir / 'c')}], {}, start=True)
    db.cancel_scan_job(cancelled_job['id'], scanqueue.summarize_job)

    _, client = _make_client()
    cleared = client.delete('/tasks/completed').json()
    cleared_ids = {t['id'] for t in cleared}
    assert cancelled_job['id'] in cleared_ids
    assert failed_job['id'] not in cleared_ids

    assert db.get_scan_job(cancelled_job['id']) is None
    assert db.get_scan_job(failed_job['id']) is not None  # FAILED jobs stay
