import os
from pathlib import Path

import pytest
from sqlalchemy.engine import Transaction

import fileops.service as svc
from db.database import Database
from db.engine import get_engine
from db.models import (
    clip_previews_table, file_details_table, file_keywords_table, file_operations_table,
    files_table, keywords_table, list_items_table, lists_table, locations_table,
    path_conflicts_table, photo_details_table, video_details_table,
)
from fileops.trash import is_in_trash


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_fileops_service.py)
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


def _count_table(table):
    from sqlalchemy import func, select
    with get_engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


# ---------------------------------------------------------------------------
# File + sidecars: trashed, tracking dropped, Locations/Keywords survive
# ---------------------------------------------------------------------------

def test_delete_file_with_sidecars_moves_to_trash_and_drops_tracking(db, root_dir, monkeypatch):
    monkeypatch.setenv('BROWSER_HIDDEN_EXTENSIONS', '.xmp')
    src = root_dir / 'a' / 'clip.mov'
    sidecar = root_dir / 'a' / 'clip.xmp'
    _mkfile(src)
    _mkfile(sidecar)
    md5 = 'filehash'
    sidecar_md5 = 'sidecarhash'
    _insert_file_row(str(root_dir / 'a'), 'clip.mov', md5, media_type='video')
    _insert_file_row(str(root_dir / 'a'), 'clip.xmp', sidecar_md5, media_type=None)

    with get_engine().begin() as conn:
        conn.execute(locations_table.insert().values(id=1, name='Kyoto'))
        conn.execute(file_details_table.insert().values(md5_hash=md5, location_id=1))
        conn.execute(keywords_table.insert().values(id=1, keyword='sunset'))
        conn.execute(file_keywords_table.insert().values(md5_hash=md5, keyword_id=1))
        conn.execute(lists_table.insert().values(id=1, name='Favorites'))
        conn.execute(list_items_table.insert().values(list_id=1, md5_hash=md5, item_code='ABC123'))
        conn.execute(clip_previews_table.insert().values(md5_hash=md5, data=b'jpeg'))
        conn.execute(video_details_table.insert().values(md5_hash=md5))
        conn.execute(path_conflicts_table.insert().values(
            md5_hash=sidecar_md5, candidate_path=str(src), source='scan'))

    result = svc.delete_paths([str(src)])

    assert len(result.results) == 1
    item = result.results[0]
    assert item.ok is True
    assert item.trash_path == str(Path(result.trash_batch) / 'a' / 'clip.mov')
    assert Path(item.trash_path).exists()
    assert (Path(result.trash_batch) / 'a' / 'clip.xmp').exists()
    assert not src.exists()
    assert not sidecar.exists()
    assert is_in_trash(item.trash_path)

    # Tracking gone for both hashes.
    assert _get_file_row(str(root_dir / 'a'), 'clip.mov') is None
    assert _get_file_row(str(root_dir / 'a'), 'clip.xmp') is None
    assert _count_table(file_details_table) == 0
    assert _count_table(video_details_table) == 0
    assert _count_table(clip_previews_table) == 0
    assert _count_table(file_keywords_table) == 0
    assert _count_table(list_items_table) == 0
    assert _count_table(path_conflicts_table) == 0

    # Locations and Keywords themselves survive.
    assert _count_table(locations_table) == 1
    assert _count_table(keywords_table) == 1


def test_delete_untracked_file_is_only_moved(db, root_dir):
    src = root_dir / 'untracked.jpg'
    _mkfile(src)

    result = svc.delete_paths([str(src)])

    assert result.results[0].ok is True
    assert Path(result.results[0].trash_path).exists()
    assert not src.exists()


# ---------------------------------------------------------------------------
# Directory delete: /a/b vs /a/bc
# ---------------------------------------------------------------------------

def test_delete_directory_removes_tracked_rows_but_not_sibling_with_shared_prefix(db, root_dir):
    dir_b = root_dir / 'a' / 'b'
    dir_bc = root_dir / 'a' / 'bc'
    _mkfile(dir_b / 'photo.jpg')
    _mkfile(dir_b / 'sub' / 'clip.mov')
    _mkfile(dir_bc / 'sibling.jpg')
    _insert_file_row(str(dir_b), 'photo.jpg', 'h1')
    _insert_file_row(str(dir_b / 'sub'), 'clip.mov', 'h2', media_type='video')
    _insert_file_row(str(dir_bc), 'sibling.jpg', 'h3')

    result = svc.delete_paths([str(dir_b)])

    assert result.results[0].ok is True
    assert not dir_b.exists()
    assert dir_bc.exists()
    # The sibling with the shared string prefix must survive untouched.
    assert _get_file_row(str(dir_bc), 'sibling.jpg') is not None
    assert _get_file_row(str(dir_b), 'photo.jpg') is None
    assert _get_file_row(str(dir_b / 'sub'), 'clip.mov') is None

    trashed = Path(result.trash_batch) / 'a' / 'b'
    assert trashed.exists()
    assert (trashed / 'photo.jpg').exists()
    assert (trashed / 'sub' / 'clip.mov').exists()


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def test_preview_delete_counts_files_tracked_sidecars_lists_and_keywords(db, root_dir, monkeypatch):
    monkeypatch.setenv('BROWSER_HIDDEN_EXTENSIONS', '.xmp')
    src = root_dir / 'clip.mov'
    sidecar = root_dir / 'clip.xmp'
    _mkfile(src)
    _mkfile(sidecar)
    md5 = 'h1'
    _insert_file_row(str(root_dir), 'clip.mov', md5, media_type='video')

    with get_engine().begin() as conn:
        conn.execute(keywords_table.insert().values(id=1, keyword='beach'))
        conn.execute(file_keywords_table.insert().values(md5_hash=md5, keyword_id=1))
        conn.execute(lists_table.insert().values(id=1, name='Favorites'))
        conn.execute(list_items_table.insert().values(list_id=1, md5_hash=md5, item_code='ABC123'))

    preview = svc.preview_delete([str(src)])

    assert preview.file_count == 2  # main file + sidecar
    assert preview.tracked_count == 1
    assert preview.sidecars == [str(sidecar)]
    assert preview.keyword_count == 1
    assert preview.list_item_count == 1


# ---------------------------------------------------------------------------
# Failure handling: rename failure rolls DB back; commit failure renames back
# ---------------------------------------------------------------------------

def test_rename_failure_during_delete_leaves_tracking_intact(db, root_dir, monkeypatch):
    src = root_dir / 'a.jpg'
    _mkfile(src)
    _insert_file_row(str(root_dir), 'a.jpg', 'h1')

    def boom(_a, _b):
        raise OSError('disk exploded')

    monkeypatch.setattr(os, 'rename', boom)

    result = svc.delete_paths([str(src)])

    assert result.results[0].ok is False
    assert src.exists()
    assert _get_file_row(str(root_dir), 'a.jpg') is not None

    with get_engine().connect() as conn:
        from sqlalchemy import select
        rows = conn.execute(select(file_operations_table)).fetchall()
    assert len(rows) == 1
    assert rows[0]._asdict()['status'] == 'failed'
    assert rows[0]._asdict()['kind'] == 'file_trash'


def test_commit_failure_during_delete_renames_file_back(db, root_dir, monkeypatch):
    src = root_dir / 'a.jpg'
    _mkfile(src)
    _insert_file_row(str(root_dir), 'a.jpg', 'h1')

    original_commit = Transaction.commit
    call_count = {'n': 0}

    def flaky_commit(self):
        call_count['n'] += 1
        # 1st commit = the journal 'pending' insert; 2nd = the guarded
        # rename's own transaction, which we want to fail.
        if call_count['n'] == 2:
            raise RuntimeError('commit boom')
        return original_commit(self)

    monkeypatch.setattr(Transaction, 'commit', flaky_commit)
    try:
        with pytest.raises(RuntimeError):
            svc.delete_paths([str(src)])
    finally:
        monkeypatch.setattr(Transaction, 'commit', original_commit)

    assert src.exists()
    assert _get_file_row(str(root_dir), 'a.jpg') is not None

    with get_engine().connect() as conn:
        from sqlalchemy import select
        rows = conn.execute(select(file_operations_table)).fetchall()
    assert rows[0]._asdict()['status'] == 'rolled_back'


# ---------------------------------------------------------------------------
# Recovery: file_trash + dir_trash, both directions
# ---------------------------------------------------------------------------

def test_recovery_finishes_pending_file_trash_when_target_exists(db, root_dir):
    source = root_dir / 'a.jpg'
    target = root_dir / '.trash' / 'batch' / 'a.jpg'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'x')
    # Source never existed on disk (crash right after os.rename succeeded,
    # before the journal row could be marked 'done') — DB still tracks it.
    _insert_file_row(str(root_dir), 'a.jpg', 'h1')

    op_id = Database().insert_file_operation('file_trash', str(source), str(target))

    svc.recover_pending_operations()

    assert Database().get_pending_file_operations() == []
    assert _get_file_row(str(root_dir), 'a.jpg') is None

    from sqlalchemy import select
    with get_engine().connect() as conn:
        row = conn.execute(select(file_operations_table)
                           .where(file_operations_table.c.id == op_id)).fetchone()
    assert row._asdict()['status'] == 'done'


def test_recovery_rolls_back_pending_file_trash_when_source_exists(db, root_dir):
    source = root_dir / 'a.jpg'
    _mkfile(source)
    target = root_dir / '.trash' / 'batch' / 'a.jpg'
    # DB was never touched (the whole apply_updates+do_rename transaction
    # never committed) — still tracked at the original path.
    _insert_file_row(str(root_dir), 'a.jpg', 'h1')

    op_id = Database().insert_file_operation('file_trash', str(source), str(target))

    svc.recover_pending_operations()

    assert Database().get_pending_file_operations() == []
    assert _get_file_row(str(root_dir), 'a.jpg') is not None

    from sqlalchemy import select
    with get_engine().connect() as conn:
        row = conn.execute(select(file_operations_table)
                           .where(file_operations_table.c.id == op_id)).fetchone()
    assert row._asdict()['status'] == 'rolled_back'


def test_recovery_finishes_pending_dir_trash_when_target_exists(db, root_dir):
    source_dir = root_dir / 'folder'
    target_dir = root_dir / '.trash' / 'batch' / 'folder'
    target_dir.mkdir(parents=True)
    (target_dir / 'photo.jpg').write_bytes(b'x')
    _insert_file_row(str(source_dir), 'photo.jpg', 'h1')

    op_id = Database().insert_file_operation('dir_trash', str(source_dir), str(target_dir))

    svc.recover_pending_operations()

    assert _get_file_row(str(source_dir), 'photo.jpg') is None

    from sqlalchemy import select
    with get_engine().connect() as conn:
        row = conn.execute(select(file_operations_table)
                           .where(file_operations_table.c.id == op_id)).fetchone()
    assert row._asdict()['status'] == 'done'


def test_recovery_rolls_back_pending_dir_trash_when_source_exists(db, root_dir):
    source_dir = root_dir / 'folder'
    _mkfile(source_dir / 'photo.jpg')
    target_dir = root_dir / '.trash' / 'batch' / 'folder'
    _insert_file_row(str(source_dir), 'photo.jpg', 'h1')

    op_id = Database().insert_file_operation('dir_trash', str(source_dir), str(target_dir))

    svc.recover_pending_operations()

    assert _get_file_row(str(source_dir), 'photo.jpg') is not None

    from sqlalchemy import select
    with get_engine().connect() as conn:
        row = conn.execute(select(file_operations_table)
                           .where(file_operations_table.c.id == op_id)).fetchone()
    assert row._asdict()['status'] == 'rolled_back'


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------

def test_delete_root_dir_itself_is_rejected(db, root_dir):
    result = svc.delete_paths([str(root_dir)])
    assert result.results[0].ok is False
    assert root_dir.exists()


def test_delete_trash_dir_itself_is_rejected(db, root_dir):
    trash_dir = root_dir / '.trash'
    trash_dir.mkdir()
    result = svc.delete_paths([str(trash_dir)])
    assert result.results[0].ok is False
    assert trash_dir.exists()


def test_delete_path_inside_trash_is_rejected(db, root_dir):
    trash_dir = root_dir / '.trash'
    nested = trash_dir / 'already_trashed' / 'x.jpg'
    _mkfile(nested)
    result = svc.delete_paths([str(nested)])
    assert result.results[0].ok is False
    assert nested.exists()


def test_delete_outside_root_dir_is_rejected(db, root_dir):
    result = svc.delete_paths(['/etc/passwd'])
    assert result.results[0].ok is False


def test_delete_lock_conflict_is_reported(db, root_dir):
    from fileops.pathlocks import try_exclusive

    src = root_dir / 'a.jpg'
    _mkfile(src)
    with try_exclusive([str(src)]):
        result = svc.delete_paths([str(src)])

    assert result.results[0].ok is False
    assert 'locked' in result.results[0].error.lower()
    assert src.exists()


def test_batch_dir_removed_when_nothing_succeeds(db, root_dir):
    missing = root_dir / 'does_not_exist.jpg'
    result = svc.delete_paths([str(missing)])
    assert result.results[0].ok is False
    assert not Path(result.trash_batch).exists()


# ---------------------------------------------------------------------------
# .trash-old must never be mistaken for the trash.
# ---------------------------------------------------------------------------

def test_dot_trash_old_sibling_is_not_treated_as_trash(root_dir):
    sibling = root_dir / '.trash-old'
    sibling.mkdir()
    assert not is_in_trash(sibling)
    assert is_in_trash(root_dir / '.trash')


# ---------------------------------------------------------------------------
# Move/rename into the trash is rejected.
# ---------------------------------------------------------------------------

def test_move_into_trash_is_rejected(db, root_dir):
    trash_dir = root_dir / '.trash'
    trash_dir.mkdir()
    src = root_dir / 'a.jpg'
    _mkfile(src)
    _insert_file_row(str(root_dir), 'a.jpg', 'h1')

    with pytest.raises(svc.TrashPathError):
        svc.move_paths([str(src)], str(trash_dir))

    assert src.exists()


def test_rename_trash_dir_itself_is_rejected(db, root_dir):
    trash_dir = root_dir / '.trash'
    trash_dir.mkdir()

    with pytest.raises(svc.TrashPathError):
        svc.rename_path(str(trash_dir), 'renamed')


def test_mkdir_inside_trash_is_rejected(db, root_dir):
    trash_dir = root_dir / '.trash'
    trash_dir.mkdir()

    with pytest.raises(svc.TrashPathError):
        svc.mkdir(str(trash_dir), 'sub')


# ---------------------------------------------------------------------------
# Scanning ROOT_DIR never picks up anything inside the trash.
# ---------------------------------------------------------------------------

def test_scan_directory_skips_files_inside_trash(root_dir):
    from scanner.scanner import Scanner

    (root_dir / 'photo.jpg').write_bytes(b'x')
    trashed = root_dir / '.trash' / 'batch' / 'old.jpg'
    _mkfile(trashed)

    results = Scanner().scan_directory(root_dir)

    names = {r.file_name for r in results}
    assert 'photo.jpg' in names
    assert 'old.jpg' not in names
