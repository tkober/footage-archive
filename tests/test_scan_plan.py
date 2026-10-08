"""Tests for the scan-tree planner (#138, epic #133): `scanner/walker.py`'s
stat-only directory census, and `/tracking/scan-plan`/`/tracking/scan-directory`
splitting a directory tree into one `ScanUnit` per directory, planned BEFORE any
hashing. `/tracking/scan-directory` becomes "plan + start immediately"; the old
recursive `index_files_in_directory` task it used to run is gone (migrated test
coverage for the actual hash/reconcile behaviour lives in test_tracking_scan.py,
which now drives that same code through `run_scan_unit`/`_index_candidates`
instead).

Follows tests/test_scan_queue.py's/test_scanjobs_api.py's conventions: a
throwaway FastAPI app with just the routers under test, `_wait_until` instead of
a fixed sleep, and every test that starts consumer threads stops them again
(plus the autouse `_stop_consumers_after_test` backstop)."""

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from api.scanjobs import ScanJobsApi
from api.tracking import TrackingApi
from db.engine import get_engine
from db.models import files_table, scan_units_table
from scanner.scanner import Scanner
from scanner.walker import walk_directories
from tasks import scanqueue


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(TrackingApi)
    app.include_router(ScanJobsApi)
    return TestClient(app)


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
    monkeypatch.setenv('SCAN_QUEUE_POLL_S', '0.05')


def _write(path: Path, content: bytes = b'x'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _fill(dir_path: Path, count: int):
    dir_path.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (dir_path / f'f{i}.jpg').write_bytes(f'file-{dir_path}-{i}'.encode())


def _make_example_tree(root: Path) -> None:
    """The ticket's example tree: `root` (3 files) with `Shibuya` (12) >
    `Shibuya_Sky` (40) / `Crossing` (28), and `Nara` (0 own files) >
    `Deer_Park` (55) / `Todaiji` (31) — six non-empty directories, `Nara`
    itself excluded. Plus sidecar/junk/trash/symlink noise that must never
    change any of those counts."""
    _fill(root, 3)
    _fill(root / 'Shibuya', 12)
    _fill(root / 'Shibuya' / 'Shibuya_Sky', 40)
    _fill(root / 'Shibuya' / 'Crossing', 28)
    (root / 'Nara').mkdir(parents=True, exist_ok=True)  # 0 own relevant files
    _fill(root / 'Nara' / 'Deer_Park', 55)
    _fill(root / 'Nara' / 'Todaiji', 31)

    # Sidecar/unrelated extensions never count as relevant.
    (root / 'Shibuya' / 'note.txt').write_bytes(b'not media')
    (root / 'Shibuya' / 'sidecar.xmp').write_bytes(b'sidecar')
    # AppleDouble sidecar sharing a real media extension — must still be
    # excluded by the hidden-name check, not just by extension.
    (root / '._hidden.jpg').write_bytes(b'ds-sidecar')
    (root / '.DS_Store').write_bytes(b'junk')

    # Trash — never walked at all, not even as a zero-count entry.
    trash_dir = root / '.trash' / 'batch'
    trash_dir.mkdir(parents=True, exist_ok=True)
    (trash_dir / 'trashed.jpg').write_bytes(b'trashed')

    # Symlinks (file and directory) — never counted, never descended into.
    # The file symlink's target doesn't even need to exist: a symlink's own
    # DirEntry type is neither is_file() nor is_dir() under
    # follow_symlinks=False, link target aside.
    (root / 'Shibuya' / 'linked.jpg').symlink_to(root / '_does_not_exist.jpg')
    (root / 'Shibuya' / 'Linked_Dir').symlink_to(root / 'Nara' / 'Deer_Park', target_is_directory=True)


# ---------------------------------------------------------------------------
# scanner/walker.py — stat-only census
# ---------------------------------------------------------------------------

def test_walk_directories_counts_direct_relevant_files_and_skips_junk(root_dir):
    _make_example_tree(root_dir)

    counts = {c.directory: c.media_file_count for c in walk_directories(root_dir)}

    assert counts[str(root_dir)] == 3
    assert counts[str(root_dir / 'Shibuya')] == 12
    assert counts[str(root_dir / 'Shibuya' / 'Shibuya_Sky')] == 40
    assert counts[str(root_dir / 'Shibuya' / 'Crossing')] == 28
    assert counts[str(root_dir / 'Nara')] == 0
    assert counts[str(root_dir / 'Nara' / 'Deer_Park')] == 55
    assert counts[str(root_dir / 'Nara' / 'Todaiji')] == 31

    # The trash directory and the symlinked directory never produce their
    # own entry at all (not even a zero-count one) — they're never visited.
    assert str(root_dir / '.trash') not in counts
    assert str(root_dir / '.trash' / 'batch') not in counts
    assert str(root_dir / 'Shibuya' / 'Linked_Dir') not in counts


def test_walk_directories_unreadable_subdirectory_is_skipped_not_fatal(root_dir):
    _fill(root_dir / 'ok', 2)
    unreadable = root_dir / 'locked'
    unreadable.mkdir()
    _fill(unreadable, 3)
    unreadable.chmod(0o000)
    try:
        counts = {c.directory: c.media_file_count for c in walk_directories(root_dir)}
    finally:
        unreadable.chmod(0o755)  # restore so pytest can clean up tmp_path

    assert counts[str(root_dir / 'ok')] == 2
    assert str(unreadable) not in counts
    assert counts[str(root_dir)] == 0  # no direct relevant files of its own


# ---------------------------------------------------------------------------
# POST /tracking/scan-plan
# ---------------------------------------------------------------------------

def test_scan_plan_six_units_in_alphabetical_order_nara_excluded(db, root_dir):
    _make_example_tree(root_dir)
    client = _make_client()

    resp = client.post('/tracking/scan-plan', json={'path': str(root_dir)})
    assert resp.status_code == 200
    body = resp.json()

    assert body['status'] == 'PLANNED'
    assert body['skipped'] == []

    units = body['units']
    assert len(units) == 6
    assert {u['status'] for u in units} == {'PLANNED'}
    assert all(u['tracked_file_count'] == 0 for u in units)

    expected = [
        (str(root_dir), 3),
        (str(root_dir / 'Nara' / 'Deer_Park'), 55),
        (str(root_dir / 'Nara' / 'Todaiji'), 31),
        (str(root_dir / 'Shibuya'), 12),
        (str(root_dir / 'Shibuya' / 'Crossing'), 28),
        (str(root_dir / 'Shibuya' / 'Shibuya_Sky'), 40),
    ]
    assert [(u['directory'], u['media_file_count']) for u in units] == expected
    assert [u['position'] for u in units] == list(range(6))

    directories = [u['directory'] for u in units]
    assert str(root_dir / 'Nara') not in directories


def test_scan_plan_second_plan_skips_queued_and_running_but_not_finished(db, root_dir):
    d_queued = root_dir / 'queued_dir'
    d_running = root_dir / 'running_dir'
    d_done = root_dir / 'done_dir'
    for d in (d_queued, d_running, d_done):
        _write(d / 'f.jpg')

    job, _ = db.create_scan_job(
        str(root_dir),
        [{'directory': str(d_queued)}, {'directory': str(d_running)}, {'directory': str(d_done)}],
        {}, start=True,
    )
    unit_ids = {u['directory']: u['id'] for u in job['units']}
    with get_engine().begin() as conn:
        conn.execute(
            update(scan_units_table).where(scan_units_table.c.id == unit_ids[str(d_running)])
            .values(status='RUNNING')
        )
        conn.execute(
            update(scan_units_table).where(scan_units_table.c.id == unit_ids[str(d_done)])
            .values(status='DONE', finished_at=datetime.now(timezone.utc))
        )

    client = _make_client()
    resp = client.post('/tracking/scan-plan', json={'path': str(root_dir)})
    assert resp.status_code == 200
    body = resp.json()

    skipped_dirs = {s['directory'] for s in body['skipped']}
    assert skipped_dirs == {str(d_queued), str(d_running)}
    assert {s['reason'] for s in body['skipped']} == {'already queued'}

    planned_dirs = {u['directory'] for u in body['units']}
    assert planned_dirs == {str(d_done)}  # finished directory is planned again


def test_scan_plan_outside_root_dir_is_403(db, root_dir):
    client = _make_client()
    resp = client.post('/tracking/scan-plan', json={'path': '/etc'})
    assert resp.status_code == 403


def test_scan_plan_non_directory_is_400(db, root_dir):
    client = _make_client()
    f = root_dir / 'f.jpg'
    _write(f)
    resp = client.post('/tracking/scan-plan', json={'path': str(f)})
    assert resp.status_code == 400


def test_scan_plan_inside_trash_is_400(db, root_dir):
    client = _make_client()
    trash_sub = root_dir / '.trash' / 'batch'
    trash_sub.mkdir(parents=True)
    resp = client.post('/tracking/scan-plan', json={'path': str(trash_sub)})
    assert resp.status_code == 400


def test_scan_plan_with_zero_units_is_a_valid_planned_job(db, root_dir):
    (root_dir / 'empty').mkdir()
    client = _make_client()
    body = client.post('/tracking/scan-plan', json={'path': str(root_dir)}).json()
    assert body['status'] == 'PLANNED'
    assert body['units'] == []
    assert body['skipped'] == []


# ---------------------------------------------------------------------------
# Before start: deselect/reselect, move-top (#137) · start · discard
# ---------------------------------------------------------------------------

def test_scan_plan_start_sets_only_non_deselected_units_to_queued(db, root_dir):
    _write(root_dir / 'd0' / 'f.jpg')
    _write(root_dir / 'd1' / 'f.jpg')
    client = _make_client()

    job = client.post('/tracking/scan-plan', json={'path': str(root_dir)}).json()
    units = job['units']
    assert len(units) == 2
    deselect_id, keep_id = units[0]['id'], units[1]['id']

    deselect_resp = client.post(f"/scan-jobs/{job['id']}/units/{deselect_id}/deselect")
    assert deselect_resp.status_code == 200
    by_id = {u['id']: u for u in deselect_resp.json()['units']}
    assert by_id[deselect_id]['status'] == 'DESELECTED'

    started = client.post(f"/scan-jobs/{job['id']}/start").json()
    assert started['status'] == 'QUEUED'
    by_id = {u['id']: u for u in started['units']}
    assert by_id[deselect_id]['status'] == 'DESELECTED'  # untouched by start
    assert by_id[keep_id]['status'] == 'QUEUED'


def test_scan_plan_can_be_discarded_while_nothing_runs(db, root_dir):
    _write(root_dir / 'd0' / 'f.jpg')
    client = _make_client()
    job = client.post('/tracking/scan-plan', json={'path': str(root_dir)}).json()

    resp = client.delete(f"/scan-jobs/{job['id']}")
    assert resp.status_code == 204
    assert client.get(f"/scan-jobs/{job['id']}").status_code == 404


# ---------------------------------------------------------------------------
# End-to-end: plan + start + drain, compatibility endpoint
# ---------------------------------------------------------------------------

def test_scan_plan_start_drain_matches_full_recursive_scan(db, root_dir, monkeypatch, fast_poll):
    import api.tracking as tracking
    monkeypatch.setattr(tracking, '_probe_and_save', lambda *a, **k: None)

    tree = {
        root_dir: ['r0.jpg', 'r1.jpg'],
        root_dir / 'a': ['a0.jpg', 'a1.mov'],
        root_dir / 'a' / 'b': ['b0.jpg'],
        root_dir / 'c': ['c0.jpg', 'c1.jpg', 'c2.jpg'],
    }
    for d, names in tree.items():
        for i, name in enumerate(names):
            _write(d / name, f'{d}/{name}-{i}'.encode())

    client = _make_client()
    plan = client.post('/tracking/scan-plan', json={'path': str(root_dir)}).json()
    assert plan['status'] == 'PLANNED'
    assert len(plan['units']) == 4  # root, a, a/b, c all have >0 direct relevant files

    started = client.post(f"/scan-jobs/{plan['id']}/start").json()
    assert started['status'] == 'QUEUED'

    scanqueue.start_consumers(2)
    try:
        _wait_until(lambda: db.get_scan_job(plan['id'])['status'] == 'DONE')
    finally:
        scanqueue.stop_consumers()

    with get_engine().connect() as conn:
        rows = conn.execute(
            select(files_table.c.directory, files_table.c.file_name, files_table.c.md5_hash)
        ).fetchall()
    tracked = {(r.directory, r.file_name): r.md5_hash for r in rows}

    expected_paths = {(str(d), name) for d, names in tree.items() for name in names}
    assert set(tracked.keys()) == expected_paths

    expected_hashes = {
        (sc.directory, sc.file_name): sc.md5_hash for sc in Scanner().scan_directory(root_dir)
    }
    assert tracked == expected_hashes


def test_scan_directory_plans_and_starts_immediately(db, root_dir, monkeypatch, fast_poll):
    """The compatibility endpoint (#138): one POST does plan + start, the
    frontend's existing toast/poll flow keeps working off the returned job
    id without ever knowing it's a plan underneath."""
    import api.tracking as tracking
    monkeypatch.setattr(tracking, '_probe_and_save', lambda *a, **k: None)

    _write(root_dir / 'a.jpg', b'a-bytes')
    _write(root_dir / 'sub' / 'b.jpg', b'b-bytes')

    client = _make_client()
    resp = client.post('/tracking/scan-directory', json={'path': str(root_dir)})
    assert resp.status_code == 200
    job_id = resp.json()
    assert isinstance(job_id, str)

    job = client.get(f'/scan-jobs/{job_id}').json()
    assert job['status'] == 'QUEUED'
    assert len(job['units']) == 2
    assert {u['status'] for u in job['units']} == {'QUEUED'}

    scanqueue.start_consumers(1)
    try:
        _wait_until(lambda: db.get_scan_job(job_id)['status'] == 'DONE')
    finally:
        scanqueue.stop_consumers()

    with get_engine().connect() as conn:
        rows = conn.execute(select(files_table.c.file_name)).fetchall()
    assert {r.file_name for r in rows} == {'a.jpg', 'b.jpg'}


def test_scan_directory_with_zero_units_still_returns_a_finished_job(db, root_dir):
    (root_dir / 'empty').mkdir()
    client = _make_client()

    resp = client.post('/tracking/scan-directory', json={'path': str(root_dir)})
    assert resp.status_code == 200
    job_id = resp.json()

    job = client.get(f'/scan-jobs/{job_id}').json()
    assert job['status'] == 'DONE'
    assert job['units'] == []


def test_scan_directory_inside_trash_is_still_400(db, root_dir):
    client = _make_client()
    trash_sub = root_dir / '.trash' / 'batch'
    trash_sub.mkdir(parents=True)
    resp = client.post('/tracking/scan-directory', json={'path': str(trash_sub)})
    assert resp.status_code == 400


def test_scan_directory_outside_root_dir_is_403(db, root_dir):
    """New behaviour vs. the old scan-directory (#138): rolling it onto the
    shared planner/validator means it now also enforces ROOT_DIR, closing a
    gap scan-plan/rediscover already covered."""
    client = _make_client()
    resp = client.post('/tracking/scan-directory', json={'path': '/etc'})
    assert resp.status_code == 403
