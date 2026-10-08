import os
from pathlib import Path

import pytest
from sqlalchemy.engine import Transaction

import fileops.service as svc
from db.database import Database
from db.engine import get_engine
from db.models import file_details_table, file_keywords_table, files_table, keywords_table, \
    list_items_table, lists_table, locations_table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mkfile(path: Path, content: bytes = b'x'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _insert_file_row(directory: str, file_name: str, md5_hash: str, media_type='photo'):
    with get_engine().begin() as conn:
        conn.execute(files_table.insert().values(
            md5_hash=md5_hash, file_name=file_name,
            file_extension=Path(file_name).suffix, media_type=media_type,
            directory=directory,
        ))


def _get_file_row(directory: str, file_name: str):
    from sqlalchemy import select
    stmt = select(files_table).where(files_table.c.directory == directory,
                                     files_table.c.file_name == file_name)
    with get_engine().connect() as conn:
        row = conn.execute(stmt).fetchone()
    return row._asdict() if row else None


def _count_files_table():
    from sqlalchemy import func, select
    with get_engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(files_table)).scalar_one()


# ---------------------------------------------------------------------------
# Directory prefix update: /a/b vs /a/bc
# ---------------------------------------------------------------------------

def test_directory_prefix_update_does_not_touch_sibling_with_shared_prefix(db):
    _insert_file_row('/a/b', 'inside.jpg', 'hash1')
    _insert_file_row('/a/b/sub', 'nested.jpg', 'hash2')
    _insert_file_row('/a/bc', 'sibling.jpg', 'hash3')

    with get_engine().begin() as conn:
        count = db.update_directory_prefix_on_conn(conn, '/a/b', '/a/renamed')

    assert count == 2
    assert _get_file_row('/a/renamed', 'inside.jpg') is not None
    assert _get_file_row('/a/renamed/sub', 'nested.jpg') is not None
    # /a/bc must be completely unaffected.
    assert _get_file_row('/a/bc', 'sibling.jpg') is not None
    assert _get_file_row('/a/b', 'inside.jpg') is None


# ---------------------------------------------------------------------------
# Service-level: directory rename moves tracked rows, validation
# ---------------------------------------------------------------------------

def test_rename_directory_updates_tracked_rows_recursively(db, root_dir):
    src_dir = root_dir / 'old_name'
    _mkfile(src_dir / 'photo.jpg')
    _mkfile(src_dir / 'sub' / 'clip.mov')
    _insert_file_row(str(src_dir), 'photo.jpg', 'h1')
    _insert_file_row(str(src_dir / 'sub'), 'clip.mov', 'h2')

    new_path = svc.rename_path(str(src_dir), 'new_name')

    assert Path(new_path) == root_dir / 'new_name'
    assert not src_dir.exists()
    assert (root_dir / 'new_name' / 'photo.jpg').exists()
    assert (root_dir / 'new_name' / 'sub' / 'clip.mov').exists()
    assert _get_file_row(str(root_dir / 'new_name'), 'photo.jpg') is not None
    assert _get_file_row(str(root_dir / 'new_name' / 'sub'), 'clip.mov') is not None
    assert _get_file_row(str(src_dir), 'photo.jpg') is None


def test_outside_root_dir_is_rejected(db, root_dir):
    outside = Path('/etc/passwd')
    with pytest.raises(svc.OutsideRootError):
        svc.rename_path(str(outside), 'new_name')


def test_moving_directory_into_its_own_child_is_rejected(db, root_dir):
    src_dir = root_dir / 'parent'
    child = src_dir / 'child'
    _mkfile(child / 'file.txt')

    results = svc.move_paths([str(src_dir)], str(child))
    assert len(results) == 1
    assert results[0].ok is False
    assert 'itself' in results[0].error or 'descendant' in results[0].error


def test_target_already_exists_is_rejected_and_nothing_changes(db, root_dir):
    src = root_dir / 'a.jpg'
    _mkfile(src)
    target = root_dir / 'b.jpg'
    _mkfile(target)
    _insert_file_row(str(root_dir), 'a.jpg', 'h1')

    with pytest.raises(svc.AlreadyExistsError):
        svc.rename_path(str(src), 'b.jpg')

    assert src.exists()
    assert target.exists()
    assert _get_file_row(str(root_dir), 'a.jpg') is not None


# ---------------------------------------------------------------------------
# File rename: keywords / location / list membership survive, sidecars move
# ---------------------------------------------------------------------------

def test_file_rename_keeps_keywords_location_and_list_membership(db, root_dir):
    src = root_dir / 'video.mov'
    _mkfile(src)
    md5 = 'keephash'
    _insert_file_row(str(root_dir), 'video.mov', md5, media_type='video')

    with get_engine().begin() as conn:
        conn.execute(locations_table.insert().values(id=1, name='Kyoto'))
        conn.execute(file_details_table.insert().values(md5_hash=md5, location_id=1))
        conn.execute(keywords_table.insert().values(id=1, keyword='sunset'))
        conn.execute(file_keywords_table.insert().values(md5_hash=md5, keyword_id=1))
        conn.execute(lists_table.insert().values(id=1, name='Favorites'))
        conn.execute(list_items_table.insert().values(list_id=1, md5_hash=md5, item_code='ABC123'))

    new_path = svc.rename_path(str(src), 'renamed.mov')

    assert Path(new_path) == root_dir / 'renamed.mov'
    row = _get_file_row(str(root_dir), 'renamed.mov')
    assert row is not None
    assert row['md5_hash'] == md5

    assert db.get_keywords(md5) == ['sunset']
    loc = db.get_location_for_file(md5)
    assert loc is not None and loc['name'] == 'Kyoto'
    lists = db.get_lists_for_file(md5)
    assert len(lists) == 1 and lists[0]['name'] == 'Favorites'


def test_sidecar_moves_along_on_file_rename(db, root_dir, monkeypatch):
    monkeypatch.setenv('BROWSER_HIDDEN_EXTENSIONS', '.xmp,.lrv')
    src = root_dir / 'clip.mov'
    sidecar = root_dir / 'clip.xmp'
    unrelated = root_dir / 'clip_other.xmp'
    _mkfile(src)
    _mkfile(sidecar)
    _mkfile(unrelated)

    svc.rename_path(str(src), 'renamed.mov')

    assert (root_dir / 'renamed.mov').exists()
    assert (root_dir / 'renamed.xmp').exists()
    assert not sidecar.exists()
    assert unrelated.exists()  # different stem — must not move


def test_shared_sidecar_stays_on_file_rename(db, root_dir, monkeypatch):
    # #154: P1.xmp belongs to P1.RW2 as much as to P1.on1 — renaming the
    # .on1 must not drag it away from the RW2.
    monkeypatch.setenv('BROWSER_HIDDEN_EXTENSIONS', '.xmp')
    raw = root_dir / 'P1.RW2'
    on1 = root_dir / 'P1.on1'
    xmp = root_dir / 'P1.xmp'
    for f in (raw, on1, xmp):
        _mkfile(f)

    svc.rename_path(str(on1), 'P2.on1')

    assert (root_dir / 'P2.on1').exists()
    assert raw.exists()
    assert xmp.exists()
    assert not (root_dir / 'P2.xmp').exists()


def test_sidecar_ownership_ignores_system_junk_with_same_stem(db, root_dir, monkeypatch):
    # A BROWSER_HIDDEN_NAMES match with the same stem is no owner — the
    # sidecar still travels along.
    monkeypatch.setenv('BROWSER_HIDDEN_EXTENSIONS', '.xmp')
    monkeypatch.setenv('BROWSER_HIDDEN_NAMES', 'clip.db')
    src = root_dir / 'clip.mov'
    _mkfile(src)
    _mkfile(root_dir / 'clip.xmp')
    _mkfile(root_dir / 'clip.db')

    svc.rename_path(str(src), 'renamed.mov')

    assert (root_dir / 'renamed.xmp').exists()


@pytest.mark.parametrize('order', ['raw_first', 'on1_first'])
def test_moving_all_owners_takes_shared_sidecar_along(db, root_dir, monkeypatch, order):
    monkeypatch.setenv('BROWSER_HIDDEN_EXTENSIONS', '.xmp')
    raw = root_dir / 'P1.RW2'
    on1 = root_dir / 'P1.on1'
    xmp = root_dir / 'P1.xmp'
    for f in (raw, on1, xmp):
        _mkfile(f)
    target = root_dir / 'target'
    target.mkdir()
    paths = [str(raw), str(on1)] if order == 'raw_first' else [str(on1), str(raw)]

    preview = svc.preview_move(paths, str(target))
    results = svc.move_paths(paths, str(target))

    assert preview.file_count == 3
    assert preview.sidecars == [str(xmp)]
    assert all(r.ok for r in results)
    assert (target / 'P1.RW2').exists()
    assert (target / 'P1.on1').exists()
    assert (target / 'P1.xmp').exists()
    assert not xmp.exists()


def test_preview_move_leaves_out_sidecar_shared_with_unselected_file(db, root_dir, monkeypatch):
    monkeypatch.setenv('BROWSER_HIDDEN_EXTENSIONS', '.xmp')
    on1 = root_dir / 'P1.on1'
    for f in (root_dir / 'P1.RW2', on1, root_dir / 'P1.xmp'):
        _mkfile(f)
    target = root_dir / 'target'
    target.mkdir()

    preview = svc.preview_move([str(on1)], str(target))

    assert preview.file_count == 1
    assert preview.sidecars == []


def test_sidecar_collision_at_target_aborts_whole_operation(db, root_dir, monkeypatch):
    monkeypatch.setenv('BROWSER_HIDDEN_EXTENSIONS', '.xmp')
    src = root_dir / 'clip.mov'
    sidecar = root_dir / 'clip.xmp'
    _mkfile(src)
    _mkfile(sidecar)
    # Something already sits at the sidecar's target path.
    _mkfile(root_dir / 'renamed.xmp')

    with pytest.raises(svc.SidecarConflictError):
        svc.rename_path(str(src), 'renamed.mov')

    # Nothing should have moved — not even the main file.
    assert src.exists()
    assert sidecar.exists()
    assert not (root_dir / 'renamed.mov').exists()


# ---------------------------------------------------------------------------
# Journal + failure handling
# ---------------------------------------------------------------------------

def test_os_rename_failure_leaves_db_unchanged_and_journals_failed(db, root_dir, monkeypatch):
    src = root_dir / 'a.jpg'
    _mkfile(src)
    _insert_file_row(str(root_dir), 'a.jpg', 'h1')

    def boom(_a, _b):
        raise OSError('disk exploded')

    monkeypatch.setattr(os, 'rename', boom)

    with pytest.raises(OSError):
        svc.rename_path(str(src), 'b.jpg')

    assert src.exists()
    assert not (root_dir / 'b.jpg').exists()
    assert _get_file_row(str(root_dir), 'a.jpg') is not None
    assert _get_file_row(str(root_dir), 'b.jpg') is None

    pending = db.get_pending_file_operations()
    assert pending == []
    from sqlalchemy import select
    with get_engine().connect() as conn:
        from db.models import file_operations_table
        rows = conn.execute(select(file_operations_table)).fetchall()
    assert len(rows) == 1
    assert rows[0]._asdict()['status'] == 'failed'


def test_commit_failure_after_rename_renames_file_back_and_marks_rolled_back(db, root_dir, monkeypatch):
    src = root_dir / 'a.jpg'
    _mkfile(src)
    _insert_file_row(str(root_dir), 'a.jpg', 'h1')

    original_commit = Transaction.commit
    call_count = {'n': 0}

    def flaky_commit(self):
        call_count['n'] += 1
        # 1st commit = the journal 'pending' insert; 2nd = the guarded
        # rename's own transaction, which we want to fail specifically.
        if call_count['n'] == 2:
            raise RuntimeError('commit boom')
        return original_commit(self)

    monkeypatch.setattr(Transaction, 'commit', flaky_commit)

    with pytest.raises(RuntimeError):
        svc.rename_path(str(src), 'b.jpg')

    monkeypatch.setattr(Transaction, 'commit', original_commit)

    # The physical rename must have been reversed.
    assert src.exists()
    assert not (root_dir / 'b.jpg').exists()

    from sqlalchemy import select
    from db.models import file_operations_table
    with get_engine().connect() as conn:
        rows = conn.execute(select(file_operations_table)).fetchall()
    assert len(rows) == 1
    assert rows[0]._asdict()['status'] == 'rolled_back'


def test_database_run_guarded_rename_rollback_on_rename_failure(db):
    calls = []

    def apply_updates(conn):
        calls.append('apply')

    def do_rename():
        calls.append('rename')
        raise OSError('nope')

    def undo_rename():
        calls.append('undo')  # should NOT be called in this path

    with pytest.raises(OSError):
        db.run_guarded_rename(apply_updates, do_rename, undo_rename)

    assert calls == ['apply', 'rename']


def test_database_run_guarded_rename_undoes_on_commit_failure(db, monkeypatch):
    original_commit = Transaction.commit

    def failing_commit(self):
        raise RuntimeError('commit boom')

    calls = []

    def apply_updates(conn):
        calls.append('apply')

    def do_rename():
        calls.append('rename')

    def undo_rename():
        calls.append('undo')

    monkeypatch.setattr(Transaction, 'commit', failing_commit)
    try:
        with pytest.raises(RuntimeError):
            db.run_guarded_rename(apply_updates, do_rename, undo_rename)
    finally:
        monkeypatch.setattr(Transaction, 'commit', original_commit)

    assert calls == ['apply', 'rename', 'undo']


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def test_recovery_marks_done_when_target_exists_on_disk(db, root_dir):
    target = root_dir / 'b.jpg'
    _mkfile(target)
    # Source never existed on disk (simulating a crash right after os.rename
    # succeeded but before the journal row could be marked 'done') — but the
    # DB row still points at the old path.
    _insert_file_row(str(root_dir), 'a.jpg', 'h1')

    op_id = db.insert_file_operation('file_rename', str(root_dir / 'a.jpg'), str(target))

    svc.recover_pending_operations()

    assert db.get_pending_file_operations() == []
    assert _get_file_row(str(root_dir), 'b.jpg') is not None
    assert _get_file_row(str(root_dir), 'a.jpg') is None

    from sqlalchemy import select
    from db.models import file_operations_table
    with get_engine().connect() as conn:
        row = conn.execute(select(file_operations_table)
                           .where(file_operations_table.c.id == op_id)).fetchone()
    assert row._asdict()['status'] == 'done'


def test_recovery_marks_rolled_back_when_source_exists_on_disk(db, root_dir):
    source = root_dir / 'a.jpg'
    _mkfile(source)
    # DB already updated to the (never-materialized) target.
    _insert_file_row(str(root_dir), 'b.jpg', 'h1')

    op_id = db.insert_file_operation('file_rename', str(source), str(root_dir / 'b.jpg'))

    svc.recover_pending_operations()

    assert db.get_pending_file_operations() == []
    assert _get_file_row(str(root_dir), 'a.jpg') is not None
    assert _get_file_row(str(root_dir), 'b.jpg') is None

    from sqlalchemy import select
    from db.models import file_operations_table
    with get_engine().connect() as conn:
        row = conn.execute(select(file_operations_table)
                           .where(file_operations_table.c.id == op_id)).fetchone()
    assert row._asdict()['status'] == 'rolled_back'


def test_recovery_marks_dir_move_prefix_both_directions(db, root_dir):
    old_dir = root_dir / 'old'
    new_dir = root_dir / 'new'
    _mkfile(new_dir / 'photo.jpg')
    _insert_file_row(str(old_dir), 'photo.jpg', 'h1')

    db.insert_file_operation('dir_move', str(old_dir), str(new_dir))
    svc.recover_pending_operations()

    assert _get_file_row(str(new_dir), 'photo.jpg') is not None
    assert _get_file_row(str(old_dir), 'photo.jpg') is None


# ---------------------------------------------------------------------------
# Bulk move
# ---------------------------------------------------------------------------

def test_bulk_move_allows_partial_success(db, root_dir):
    target = root_dir / 'target'
    target.mkdir()
    ok_file = root_dir / 'ok.jpg'
    _mkfile(ok_file)
    missing_file = root_dir / 'missing.jpg'  # never created

    results = svc.move_paths([str(ok_file), str(missing_file)], str(target))

    assert len(results) == 2
    by_path = {r.path: r for r in results}
    assert by_path[str(ok_file)].ok is True
    assert (target / 'ok.jpg').exists()
    assert by_path[str(missing_file)].ok is False


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def test_preview_move_counts_files_and_tracked_and_sidecars(db, root_dir, monkeypatch):
    monkeypatch.setenv('BROWSER_HIDDEN_EXTENSIONS', '.xmp')
    src = root_dir / 'clip.mov'
    sidecar = root_dir / 'clip.xmp'
    _mkfile(src)
    _mkfile(sidecar)
    _insert_file_row(str(root_dir), 'clip.mov', 'h1', media_type='video')

    target = root_dir / 'target'
    target.mkdir()

    preview = svc.preview_move([str(src)], str(target))

    assert preview.file_count == 2  # main file + sidecar
    assert preview.tracked_count == 1  # only the main file is tracked
    assert preview.sidecars == [str(sidecar)]


def test_preview_move_counts_exclude_system_files(db, root_dir):
    src_dir = root_dir / 'camera'
    _mkfile(src_dir / 'clip.mov')
    _mkfile(src_dir / '.DS_Store')
    _mkfile(src_dir / 'Thumbs.db')
    _insert_file_row(str(src_dir), 'clip.mov', 'h1', media_type='video')

    target = root_dir / 'target'
    target.mkdir()

    preview = svc.preview_move([str(src_dir)], str(target))

    assert preview.file_count == 1  # system files excluded
    assert preview.tracked_count == 1


def test_moving_directory_carries_along_system_files(db, root_dir):
    src_dir = root_dir / 'camera'
    _mkfile(src_dir / 'clip.mov')
    _mkfile(src_dir / '.DS_Store')
    _insert_file_row(str(src_dir), 'clip.mov', 'h1', media_type='video')

    target = root_dir / 'target'
    target.mkdir()

    results = svc.move_paths([str(src_dir)], str(target))

    assert results[0].ok is True
    new_dir = Path(results[0].new_path)
    assert (new_dir / 'clip.mov').exists()
    assert (new_dir / '.DS_Store').exists()  # travels along with the one physical dir rename
    assert not src_dir.exists()


# ---------------------------------------------------------------------------
# TOCTOU: target appearing between up-front validation and the physical
# rename must never be silently overwritten by os.rename.
# ---------------------------------------------------------------------------

def test_target_created_between_validation_and_rename_is_not_overwritten(db, root_dir, monkeypatch):
    src = root_dir / 'a.jpg'
    _mkfile(src, b'original')
    _insert_file_row(str(root_dir), 'a.jpg', 'h1')
    target = root_dir / 'b.jpg'

    original_insert = Database.insert_file_operation

    def sneaky_insert(self, *args, **kwargs):
        # Simulate a concurrent writer creating the target in the window
        # between the service's up-front _require_absent() check and the
        # physical os.rename — this runs after that check (it's part of
        # dispatching the rename) but before do_rename()'s own re-check.
        if not target.exists():
            target.write_bytes(b'already-here')
        return original_insert(self, *args, **kwargs)

    monkeypatch.setattr(Database, 'insert_file_operation', sneaky_insert)

    with pytest.raises(svc.AlreadyExistsError):
        svc.rename_path(str(src), 'b.jpg')

    # The pre-existing target must be untouched, and the source must not
    # have been consumed by a silent os.rename overwrite.
    assert target.read_bytes() == b'already-here'
    assert src.exists()
    assert src.read_bytes() == b'original'
    assert _get_file_row(str(root_dir), 'a.jpg') is not None
    assert _get_file_row(str(root_dir), 'b.jpg') is None


def test_sidecar_target_created_between_validation_and_rename_is_not_overwritten(db, root_dir, monkeypatch):
    monkeypatch.setenv('BROWSER_HIDDEN_EXTENSIONS', '.xmp')
    src = root_dir / 'clip.mov'
    sidecar = root_dir / 'clip.xmp'
    _mkfile(src)
    _mkfile(sidecar)
    sidecar_target = root_dir / 'renamed.xmp'

    original_insert = Database.insert_file_operation

    def sneaky_insert(self, *args, **kwargs):
        if not sidecar_target.exists():
            sidecar_target.write_bytes(b'already-here')
        return original_insert(self, *args, **kwargs)

    monkeypatch.setattr(Database, 'insert_file_operation', sneaky_insert)

    with pytest.raises(svc.AlreadyExistsError):
        svc.rename_path(str(src), 'renamed.mov')

    # Nothing should have moved — the main file's rename is reversed when a
    # later rename in the group (the sidecar) fails.
    assert src.exists()
    assert sidecar.exists()
    assert not (root_dir / 'renamed.mov').exists()
    assert sidecar_target.read_bytes() == b'already-here'


# ---------------------------------------------------------------------------
# Undo failure after a commit failure: must leave the journal 'pending'.
# ---------------------------------------------------------------------------

def test_undo_failure_after_commit_failure_leaves_journal_pending(db, root_dir, monkeypatch):
    src = root_dir / 'a.jpg'
    _mkfile(src)
    _insert_file_row(str(root_dir), 'a.jpg', 'h1')

    original_commit = Transaction.commit
    commit_calls = {'n': 0}

    def flaky_commit(self):
        commit_calls['n'] += 1
        # 1st commit = the journal 'pending' insert (must succeed); 2nd =
        # the guarded rename's own transaction, which we want to fail.
        if commit_calls['n'] == 2:
            raise RuntimeError('commit boom')
        return original_commit(self)

    original_rename = os.rename
    rename_calls = {'n': 0}

    def flaky_rename(a, b):
        rename_calls['n'] += 1
        # 1st call = the real do_rename() rename, which must succeed so we
        # reach the commit-failure path; 2nd call = undo_rename() trying to
        # reverse it, which we want to fail too.
        if rename_calls['n'] == 2:
            raise OSError('undo also fails')
        return original_rename(a, b)

    monkeypatch.setattr(Transaction, 'commit', flaky_commit)
    monkeypatch.setattr(os, 'rename', flaky_rename)

    with pytest.raises(Exception):
        svc.rename_path(str(src), 'b.jpg')

    monkeypatch.setattr(Transaction, 'commit', original_commit)
    monkeypatch.setattr(os, 'rename', original_rename)

    # The journal row must stay 'pending' — not 'rolled_back' (which would
    # claim the disk was reverted, when it wasn't) and not 'failed' (which
    # would stop recover_pending_operations() from ever looking at it).
    from sqlalchemy import select
    from db.models import file_operations_table
    with get_engine().connect() as conn:
        rows = conn.execute(select(file_operations_table)).fetchall()
    assert len(rows) == 1
    assert rows[0]._asdict()['status'] == 'pending'

    # recover_pending_operations() must still be able to fix this up later.
    svc.recover_pending_operations()
    with get_engine().connect() as conn:
        rows = conn.execute(select(file_operations_table)).fetchall()
    assert rows[0]._asdict()['status'] in ('done', 'rolled_back')


def test_run_guarded_rename_always_closes_connection_on_undo_failure(db, monkeypatch):
    from db.database import UndoRenameFailedError

    original_commit = Transaction.commit

    def failing_commit(self):
        raise RuntimeError('commit boom')

    def failing_undo():
        raise RuntimeError('undo boom')

    monkeypatch.setattr(Transaction, 'commit', failing_commit)
    try:
        with pytest.raises(UndoRenameFailedError):
            db.run_guarded_rename(lambda conn: None, lambda: None, failing_undo)
    finally:
        monkeypatch.setattr(Transaction, 'commit', original_commit)

    # The engine pool must not have leaked a connection left open by the
    # failure path — a fresh checkout must still work immediately after.
    with get_engine().connect() as conn:
        conn.execute(__import__('sqlalchemy').text('SELECT 1'))


# ---------------------------------------------------------------------------
# Name validation: '.' and '..' are never valid names.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('bad_name', ['.', '..'])
def test_validate_name_rejects_dot_and_dotdot(db, root_dir, bad_name):
    src = root_dir / 'a.jpg'
    _mkfile(src)

    with pytest.raises(svc.InvalidNameError):
        svc.rename_path(str(src), bad_name)

    with pytest.raises(svc.InvalidNameError):
        svc.mkdir(str(root_dir), bad_name)


# ---------------------------------------------------------------------------
# Non-EXDEV OSError during a bulk move must not abort the remaining items.
# ---------------------------------------------------------------------------

def test_permission_error_in_bulk_move_is_reported_per_item_not_raised(db, root_dir, monkeypatch):
    target = root_dir / 'target'
    target.mkdir()
    bad_file = root_dir / 'bad.jpg'
    ok_file = root_dir / 'ok.jpg'
    _mkfile(bad_file)
    _mkfile(ok_file)

    original_rename = os.rename

    def flaky_rename(a, b):
        if Path(a) == bad_file:
            raise PermissionError('permission denied')
        return original_rename(a, b)

    monkeypatch.setattr(os, 'rename', flaky_rename)

    results = svc.move_paths([str(bad_file), str(ok_file)], str(target))

    assert len(results) == 2
    by_path = {r.path: r for r in results}
    assert by_path[str(bad_file)].ok is False
    assert 'permission denied' in by_path[str(bad_file)].error.lower()
    assert by_path[str(ok_file)].ok is True
    assert bad_file.exists()  # untouched
    assert (target / 'ok.jpg').exists()
