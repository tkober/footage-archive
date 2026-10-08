"""Tests for #139 (epic #133): the persisted recursive directory status
(`DirectoryStats`), its recompute primitives (`tasks/directory_stats.py`'s
`refresh_directory`/`refresh_chain`/`run_census`), every writer that touches
it (scan unit, census, fileops, Track file/Rediscover), and "Scan untracked
only" (`only_untracked` on `/tracking/scan-plan`/`/scan-directory`).

Follows tests/test_scan_plan.py's/test_directory_api.py's conventions: a
throwaway FastAPI app with just the routers under test, direct `files_table`
inserts to simulate "already tracked", and `_wait_until` instead of a fixed
sleep for the consumer-thread tests."""

import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.files import FilesApi
from api.tracking import TrackingApi
from db.engine import get_engine
from db.models import files_table
from fileops import service as fileops_service
from tasks import directory_stats, scanqueue
from tasks.scan_hooks import on_unit_done


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(TrackingApi)
    app.include_router(FilesApi)
    return TestClient(app)


def _write(path: Path, content: bytes = b'x'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _insert_file_row(directory: str, file_name: str, md5_hash: str, media_type='photo'):
    with get_engine().begin() as conn:
        conn.execute(files_table.insert().values(
            md5_hash=md5_hash, file_name=file_name,
            file_extension=Path(file_name).suffix, media_type=media_type,
            directory=directory,
        ))


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


# ---------------------------------------------------------------------------
# refresh_directory / refresh_chain
# ---------------------------------------------------------------------------

def test_refresh_directory_computes_own_counts_for_a_leaf(db, root_dir):
    leaf = root_dir / 'leaf'
    _write(leaf / 'a.jpg')
    _write(leaf / 'b.jpg')
    _insert_file_row(str(leaf), 'a.jpg', 'h1')

    directory_stats.refresh_directory(str(leaf), 'scan')

    row = db.get_directory_stats(str(leaf))
    assert row['parent'] == str(root_dir)
    assert row['media_files'] == 2
    assert row['tracked_files'] == 1
    assert row['subtree_media_files'] == 2
    assert row['subtree_tracked_files'] == 1
    assert row['subtree_complete'] is True
    assert row['source'] == 'scan'
    assert row['walked_at'] is not None


def test_refresh_chain_rolls_up_every_ancestor_to_root(db, root_dir):
    leaf = root_dir / 'a' / 'b' / 'c'
    _write(leaf / 'f.jpg')
    _insert_file_row(str(leaf), 'f.jpg', 'h1')

    directory_stats.refresh_chain(str(leaf), 'scan')

    for directory in (leaf, leaf.parent, leaf.parent.parent, root_dir):
        row = db.get_directory_stats(str(directory))
        assert row is not None, f'expected a row for {directory}'
        assert row['subtree_media_files'] == 1
        assert row['subtree_tracked_files'] == 1
        assert row['subtree_complete'] is True


def test_partially_known_tree_is_partial_not_complete(db, root_dir):
    parent = root_dir / 'parent'
    known = parent / 'known'
    unknown = parent / 'unknown'
    _write(known / 'f.jpg')
    _write(unknown / 'g.jpg')
    _insert_file_row(str(known), 'f.jpg', 'h1')

    # Only `known` has ever been refreshed — `unknown` has no row at all.
    directory_stats.refresh_directory(str(known), 'scan')
    directory_stats.refresh_directory(str(parent), 'scan')

    row = db.get_directory_stats(str(parent))
    assert row['subtree_complete'] is False
    # The unknown child contributes nothing — only `known`'s file counts.
    assert row['subtree_media_files'] == 1
    assert row['subtree_tracked_files'] == 1


def test_vanished_directory_deletes_its_row_and_descendants(db, root_dir):
    parent = root_dir / 'parent'
    child = parent / 'child'
    _write(child / 'f.jpg')
    directory_stats.refresh_chain(str(child), 'scan')
    assert db.get_directory_stats(str(child)) is not None

    import shutil
    shutil.rmtree(child)
    directory_stats.refresh_directory(str(parent), 'scan')

    assert db.get_directory_stats(str(child)) is None
    row = db.get_directory_stats(str(parent))
    assert row['subtree_complete'] is True  # no subdirectories left
    assert row['subtree_media_files'] == 0


def test_stale_missing_files_row_does_not_hide_sibling_untracked(db, root_dir):
    """A Files row surviving for a file that's gone from disk (tracked_files
    > media_files in that one folder) must not cancel out a sibling
    folder's real untracked file — the whole reason subtree_tracked_files
    sums min(tracked, media) per directory instead of raw sums."""
    stale_dir = root_dir / 'stale'
    stale_dir.mkdir()
    _insert_file_row(str(stale_dir), 'gone.jpg', 'h_gone')  # no file on disk

    sibling_dir = root_dir / 'sibling'
    _write(sibling_dir / 'untracked.jpg')  # real file, never tracked

    directory_stats.refresh_chain(str(stale_dir), 'scan')
    directory_stats.refresh_chain(str(sibling_dir), 'scan')

    root_row = db.get_directory_stats(str(root_dir))
    # media=1 (sibling's file only, stale_dir has 0 on disk), tracked=0
    # (clamped: stale_dir contributes min(1, 0) = 0) — so the sibling's
    # untracked file is correctly visible, not hidden by the stale row.
    assert root_row['subtree_media_files'] == 1
    assert root_row['subtree_tracked_files'] == 0


# ---------------------------------------------------------------------------
# Writers: scan unit (on_unit_done), mkdir, rename/move, trash
# ---------------------------------------------------------------------------

def test_on_unit_done_refreshes_directory_and_parent_chain(db, root_dir):
    unit_dir = root_dir / 'parent' / 'unit'
    _write(unit_dir / 'a.jpg')
    _insert_file_row(str(unit_dir), 'a.jpg', 'h1')

    on_unit_done(str(unit_dir))

    assert db.get_directory_stats(str(unit_dir))['tracked_files'] == 1
    parent_row = db.get_directory_stats(str(unit_dir.parent))
    assert parent_row['subtree_media_files'] == 1
    assert parent_row['subtree_tracked_files'] == 1
    root_row = db.get_directory_stats(str(root_dir))
    assert root_row['subtree_media_files'] == 1


def test_mkdir_adds_an_empty_complete_row(db, root_dir):
    new_path = fileops_service.mkdir(str(root_dir), 'newfolder')

    row = db.get_directory_stats(new_path)
    assert row is not None
    assert row['media_files'] == 0
    assert row['tracked_files'] == 0
    assert row['subtree_complete'] is True
    # The parent's own row reflects the new (empty, complete) child too.
    parent_row = db.get_directory_stats(str(root_dir))
    assert parent_row['subtree_complete'] is True


def test_directory_rename_reprefixes_rows_and_refreshes_both_chains(db, root_dir):
    src = root_dir / 'old_name'
    _write(src / 'f.jpg')
    _insert_file_row(str(src), 'f.jpg', 'h1')
    directory_stats.refresh_chain(str(src), 'scan')
    assert db.get_directory_stats(str(src)) is not None

    new_path = fileops_service.rename_path(str(src), 'new_name')

    assert db.get_directory_stats(str(src)) is None
    new_row = db.get_directory_stats(new_path)
    assert new_row is not None
    assert new_row['parent'] == str(root_dir)
    assert new_row['media_files'] == 1
    assert new_row['tracked_files'] == 1
    # Old parent chain (root_dir, here the same as the new one since this
    # was a same-directory rename) was refreshed and reflects the move.
    root_row = db.get_directory_stats(str(root_dir))
    assert root_row['subtree_media_files'] == 1


def test_directory_move_reprefixes_rows_and_refreshes_old_and_new_parent(db, root_dir):
    src = root_dir / 'source_parent' / 'moved'
    dest_parent = root_dir / 'dest_parent'
    dest_parent.mkdir(parents=True)
    _write(src / 'f.jpg')
    _insert_file_row(str(src), 'f.jpg', 'h1')
    directory_stats.refresh_chain(str(src), 'scan')

    results = fileops_service.move_paths([str(src)], str(dest_parent))
    assert results[0].ok

    new_path = str(dest_parent / 'moved')
    assert db.get_directory_stats(str(src)) is None
    new_row = db.get_directory_stats(new_path)
    assert new_row is not None
    assert new_row['parent'] == str(dest_parent)

    old_parent_row = db.get_directory_stats(str(src.parent))
    assert old_parent_row['subtree_media_files'] == 0  # moved child is gone
    new_parent_row = db.get_directory_stats(str(dest_parent))
    assert new_parent_row['subtree_media_files'] == 1  # moved child landed here


def test_trashing_a_file_recounts_its_directory(db, root_dir):
    folder = root_dir / 'folder'
    _write(folder / 'a.jpg')
    _write(folder / 'b.jpg')
    _insert_file_row(str(folder), 'a.jpg', 'h1')
    _insert_file_row(str(folder), 'b.jpg', 'h2')
    directory_stats.refresh_chain(str(folder), 'scan')
    assert db.get_directory_stats(str(folder))['media_files'] == 2

    result = fileops_service.delete_paths([str(folder / 'a.jpg')])
    assert result.results[0].ok

    row = db.get_directory_stats(str(folder))
    assert row['media_files'] == 1
    assert row['tracked_files'] == 1


def test_trashing_a_directory_deletes_its_rows_and_refreshes_old_parent(db, root_dir):
    folder = root_dir / 'parent' / 'to_trash'
    _write(folder / 'a.jpg')
    _insert_file_row(str(folder), 'a.jpg', 'h1')
    directory_stats.refresh_chain(str(folder), 'scan')
    assert db.get_directory_stats(str(folder)) is not None

    result = fileops_service.delete_paths([str(folder)])
    assert result.results[0].ok

    assert db.get_directory_stats(str(folder)) is None
    parent_row = db.get_directory_stats(str(folder.parent))
    assert parent_row['subtree_media_files'] == 0
    assert parent_row['subtree_complete'] is True


# ---------------------------------------------------------------------------
# Census (run_census + POST /tracking/census)
# ---------------------------------------------------------------------------

def test_run_census_writes_one_row_per_directory_bottom_up(db, root_dir):
    _write(root_dir / 'root.jpg')
    _write(root_dir / 'shibuya' / 'f1.jpg')
    _write(root_dir / 'shibuya' / 'shibuya_sky' / 'f2.jpg')
    _write(root_dir / 'shibuya' / 'crossing' / 'f3.jpg')
    (root_dir / 'nara').mkdir()  # 0 own files, two children below
    _write(root_dir / 'nara' / 'deer_park' / 'f4.jpg')
    _write(root_dir / 'nara' / 'todaiji' / 'f5.jpg')
    _insert_file_row(str(root_dir / 'shibuya' / 'shibuya_sky'), 'f2.jpg', 'h1')

    messages = []
    directory_stats.run_census(str(root_dir), messages.append)

    sky = db.get_directory_stats(str(root_dir / 'shibuya' / 'shibuya_sky'))
    assert sky['media_files'] == 1
    assert sky['tracked_files'] == 1
    assert sky['subtree_complete'] is True

    shibuya = db.get_directory_stats(str(root_dir / 'shibuya'))
    assert shibuya['media_files'] == 1  # f1.jpg only, direct
    assert shibuya['subtree_media_files'] == 1 + 1 + 1  # own + sky + crossing
    assert shibuya['subtree_tracked_files'] == 1
    assert shibuya['subtree_complete'] is True

    nara = db.get_directory_stats(str(root_dir / 'nara'))
    assert nara['media_files'] == 0
    assert nara['subtree_media_files'] == 2  # deer_park + todaiji
    assert nara['subtree_complete'] is True

    root_row = db.get_directory_stats(str(root_dir))
    assert root_row['subtree_media_files'] == 1 + 3 + 2  # root + shibuya tree + nara tree
    assert any('Updated status' in m for m in messages)


def test_run_census_deletes_rows_for_directories_that_vanished(db, root_dir):
    gone = root_dir / 'gone'
    _write(gone / 'f.jpg')
    directory_stats.run_census(str(root_dir), lambda m: None)
    assert db.get_directory_stats(str(gone)) is not None

    import shutil
    shutil.rmtree(gone)
    directory_stats.run_census(str(root_dir), lambda m: None)

    assert db.get_directory_stats(str(gone)) is None


def test_census_endpoint_rejects_duplicate_running_census_for_same_path(db, root_dir):
    client = _make_client()
    assert directory_stats.try_start_census(str(root_dir)) is True
    try:
        resp = client.post('/tracking/census', json={'path': str(root_dir)})
        assert resp.status_code == 409
    finally:
        directory_stats.finish_census(str(root_dir))


def test_census_endpoint_runs_and_writes_rows(db, root_dir):
    _write(root_dir / 'a' / 'f.jpg')
    client = _make_client()
    resp = client.post('/tracking/census', json={'path': str(root_dir)})
    assert resp.status_code == 200
    task_id = resp.json()

    def _done():
        from tasks.taskmanager import TaskManager
        task = TaskManager().get_task(task_id)
        return task is not None and task.status in ('COMPLETED', 'FAILED')

    _wait_until(_done)
    assert db.get_directory_stats(str(root_dir / 'a')) is not None


def test_auto_census_triggers_after_a_scan_job_finishes(db, root_dir, monkeypatch, fast_poll):
    """Once the job finalizes (DONE here, one unit), tasks/scanqueue.py's
    _trigger_job_census starts a Census for the job's root_path — exactly
    once (see db/database.py's finalized-return guarantee)."""
    from api.tracking import ScanSummary

    _write(root_dir / 'f.jpg')

    def fake(directory, options, report, should_cancel):
        return ScanSummary(indexed=1)

    monkeypatch.setattr(scanqueue, '_executor', fake)
    job, _ = db.create_scan_job(str(root_dir), [{'directory': str(root_dir), 'media_file_count': 1}],
                                {}, start=True)

    scanqueue.start_consumers(1)
    try:
        _wait_until(lambda: db.get_scan_job(job['id'])['status'] == 'DONE')
        _wait_until(lambda: db.get_directory_stats(str(root_dir)) is not None)
    finally:
        scanqueue.stop_consumers()


# ---------------------------------------------------------------------------
# Concurrency: two sibling refreshes racing on their shared parent row
# ---------------------------------------------------------------------------

def test_concurrent_sibling_refreshes_leave_a_correct_parent_row(db, root_dir):
    parent = root_dir / 'parent'
    sib_a = parent / 'a'
    sib_b = parent / 'b'
    _write(sib_a / 'f1.jpg')
    _write(sib_a / 'f2.jpg')
    _write(sib_b / 'g1.jpg')
    _insert_file_row(str(sib_a), 'f1.jpg', 'h1')

    barrier = threading.Barrier(2)

    def go(directory):
        barrier.wait(timeout=5)
        directory_stats.refresh_chain(str(directory), 'scan')

    threads = [threading.Thread(target=go, args=(sib_a,)), threading.Thread(target=go, args=(sib_b,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    parent_row = db.get_directory_stats(str(parent))
    assert parent_row is not None
    assert parent_row['subtree_media_files'] == 3  # 2 (a) + 1 (b), no lost update
    assert parent_row['subtree_tracked_files'] == 1
    assert parent_row['subtree_complete'] is True


# ---------------------------------------------------------------------------
# "Scan untracked only" (only_untracked on /tracking/scan-plan|scan-directory)
# ---------------------------------------------------------------------------

def test_only_untracked_plans_no_unit_once_everything_is_tracked(db, root_dir):
    _write(root_dir / 'f.jpg')
    _insert_file_row(str(root_dir), 'f.jpg', 'h1')
    client = _make_client()

    body = client.post('/tracking/scan-plan', json={'path': str(root_dir), 'only_untracked': True}).json()
    assert body['units'] == []


def test_only_untracked_plans_exactly_one_unit_after_adding_a_file(db, root_dir):
    _write(root_dir / 'f.jpg')
    _insert_file_row(str(root_dir), 'f.jpg', 'h1')
    _write(root_dir / 'new.jpg')  # untracked addition
    client = _make_client()

    body = client.post('/tracking/scan-plan', json={'path': str(root_dir), 'only_untracked': True}).json()
    assert len(body['units']) == 1
    assert body['units'][0]['directory'] == str(root_dir)


def test_only_untracked_false_by_default_plans_the_whole_tree(db, root_dir):
    _write(root_dir / 'f.jpg')
    _insert_file_row(str(root_dir), 'f.jpg', 'h1')
    client = _make_client()

    body = client.post('/tracking/scan-plan', json={'path': str(root_dir)}).json()
    assert len(body['units']) == 1  # still planned even though fully tracked


def test_only_untracked_scan_directory_returns_a_valid_zero_unit_job(db, root_dir):
    _write(root_dir / 'f.jpg')
    _insert_file_row(str(root_dir), 'f.jpg', 'h1')
    client = _make_client()

    resp = client.post('/tracking/scan-directory', json={'path': str(root_dir), 'only_untracked': True})
    assert resp.status_code == 200
    job_id = resp.json()
    assert db.get_scan_job(job_id)['status'] == 'DONE'


# ---------------------------------------------------------------------------
# Reader: query_directory's new PathChild fields
# ---------------------------------------------------------------------------

def test_query_directory_reports_below_and_complete_once_rows_exist(db, root_dir):
    complete_dir = root_dir / 'complete_folder'
    _write(complete_dir / 'f.jpg')
    _insert_file_row(str(complete_dir), 'f.jpg', 'h1')

    below_dir = root_dir / 'below_folder'
    _write(below_dir / 'sub' / 'g.jpg')  # untracked, one level down

    directory_stats.run_census(str(root_dir), lambda m: None)

    client = _make_client()
    resp = client.post('/files/directory', json={'path': str(root_dir)})
    assert resp.status_code == 200
    by_name = {e['name']: e for e in resp.json()['items']}

    complete_entry = by_name['complete_folder']
    assert complete_entry['subtree_status'] == 'complete'
    assert complete_entry['below_untracked_count'] == 0
    assert complete_entry['status_walked_at'] is not None

    below_entry = by_name['below_folder']
    assert below_entry['subtree_status'] == 'complete'  # census walked it all
    assert below_entry['below_untracked_count'] == 1


def test_query_directory_unknown_status_without_any_row(db, root_dir):
    leaf = root_dir / 'leaf_no_subdirs'
    leaf.mkdir()  # no subdirectories, never walked -> trivially complete
    parent = root_dir / 'parent_with_subdir'
    (parent / 'child').mkdir(parents=True)  # has a real subdirectory, never walked -> unknown

    client = _make_client()
    resp = client.post('/files/directory', json={'path': str(root_dir)})
    by_name = {e['name']: e for e in resp.json()['items']}

    assert by_name['leaf_no_subdirs']['subtree_status'] == 'complete'
    assert by_name['leaf_no_subdirs']['below_untracked_count'] == 0
    assert by_name['leaf_no_subdirs']['subtree_untracked_count'] is None

    assert by_name['parent_with_subdir']['subtree_status'] == 'unknown'
    assert by_name['parent_with_subdir']['below_untracked_count'] is None
