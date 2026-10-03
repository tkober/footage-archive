"""Unit tests for the pure classification in fileops/rediscover.py, plus
DB-backed tests for apply() covering relink, conflict persistence/pruning,
track_new, and metadata preservation."""

from datetime import datetime
from pathlib import Path

from sqlalchemy import select

import fileops.rediscover as rediscover
from db.engine import get_engine
from db.models import (
    file_details_table,
    file_keywords_table,
    files_table,
    keywords_table,
    list_items_table,
    lists_table,
    path_conflicts_table,
)
from scanner.scanner import ScanResult


def _sc(md5_hash: str, directory: str, file_name: str, media_type='photo') -> ScanResult:
    return ScanResult(
        md5_hash=md5_hash,
        file_name=file_name,
        file_extension=Path(file_name).suffix,
        media_type=media_type,
        directory=directory,
        last_indexed_at=datetime.now(),
    )


def _no_exists(_path: str) -> bool:
    return False


def _all_exist(_path: str) -> bool:
    return True


# ---------------------------------------------------------------------------
# classify() — pure, no DB/filesystem
# ---------------------------------------------------------------------------

def test_classify_unknown_hash_is_new():
    scan_results = [_sc('h1', '/a', 'f.jpg')]
    result = rediscover.classify(scan_results, tracked={}, exists=_no_exists)
    assert result.new == {'h1': ['/a/f.jpg']}
    assert result.unchanged == []
    assert result.relinked == []
    assert result.conflicts == []


def test_classify_same_path_is_unchanged():
    scan_results = [_sc('h1', '/a', 'f.jpg')]
    tracked = {'h1': {'directory': '/a', 'file_name': 'f.jpg'}}
    result = rediscover.classify(scan_results, tracked, exists=_no_exists)
    assert result.unchanged == ['h1']
    assert result.conflicts == []
    assert result.relinked == []
    assert result.new == {}


def test_classify_tracked_found_with_extra_copy_is_unchanged_and_conflict():
    # rule 2 extended: tracked path still found, but so is another copy.
    scan_results = [_sc('h1', '/a', 'f.jpg'), _sc('h1', '/b', 'copy.jpg')]
    tracked = {'h1': {'directory': '/a', 'file_name': 'f.jpg'}}
    result = rediscover.classify(scan_results, tracked, exists=_no_exists)
    assert result.unchanged == ['h1']
    assert len(result.conflicts) == 1
    assert result.conflicts[0].md5_hash == 'h1'
    assert result.conflicts[0].candidate_paths == ['/b/copy.jpg']


def test_classify_old_path_still_exists_is_conflict():
    # rule 3: tracked path missing from this scan but still exists on disk
    # (e.g. scan was restricted to a subfolder not covering the old path).
    scan_results = [_sc('h1', '/new', 'f.jpg')]
    tracked = {'h1': {'directory': '/old', 'file_name': 'f.jpg'}}
    result = rediscover.classify(scan_results, tracked, exists=_all_exist)
    assert result.conflicts == [rediscover.Conflict(md5_hash='h1', candidate_paths=['/new/f.jpg'])]
    assert result.relinked == []
    assert result.unchanged == []


def test_classify_old_path_gone_single_match_is_relink():
    # rule 4
    scan_results = [_sc('h1', '/new', 'f.jpg')]
    tracked = {'h1': {'directory': '/old', 'file_name': 'f.jpg'}}
    result = rediscover.classify(scan_results, tracked, exists=_no_exists)
    assert result.relinked == [rediscover.Relink(md5_hash='h1', old_path='/old/f.jpg', new_path='/new/f.jpg')]
    assert result.conflicts == []


def test_classify_old_path_gone_multiple_matches_is_conflict():
    # rule 5
    scan_results = [_sc('h1', '/new', 'f.jpg'), _sc('h1', '/new2', 'f.jpg')]
    tracked = {'h1': {'directory': '/old', 'file_name': 'f.jpg'}}
    result = rediscover.classify(scan_results, tracked, exists=_no_exists)
    assert result.relinked == []
    assert result.conflicts == [
        rediscover.Conflict(md5_hash='h1', candidate_paths=['/new/f.jpg', '/new2/f.jpg'])
    ]


def test_classify_new_hash_found_at_multiple_paths_is_grouped():
    scan_results = [_sc('h1', '/b', 'f2.jpg'), _sc('h1', '/a', 'f1.jpg')]
    result = rediscover.classify(scan_results, tracked={}, exists=_no_exists)
    assert result.new == {'h1': ['/a/f1.jpg', '/b/f2.jpg']}  # sorted


def test_classify_handles_several_hashes_independently():
    scan_results = [
        _sc('unchanged', '/a', 'u.jpg'),
        _sc('relinked', '/new', 'r.jpg'),
        _sc('newhash', '/a', 'n.jpg'),
    ]
    tracked = {
        'unchanged': {'directory': '/a', 'file_name': 'u.jpg'},
        'relinked': {'directory': '/old', 'file_name': 'r.jpg'},
    }
    result = rediscover.classify(scan_results, tracked, exists=_no_exists)
    assert result.unchanged == ['unchanged']
    assert result.relinked == [rediscover.Relink('relinked', '/old/r.jpg', '/new/r.jpg')]
    assert result.new == {'newhash': ['/a/n.jpg']}
    assert result.conflicts == []


# ---------------------------------------------------------------------------
# apply() — DB-backed
# ---------------------------------------------------------------------------

def _insert_file_row(directory: str, file_name: str, md5_hash: str, media_type='photo'):
    with get_engine().begin() as conn:
        conn.execute(files_table.insert().values(
            md5_hash=md5_hash, file_name=file_name,
            file_extension=Path(file_name).suffix, media_type=media_type,
            directory=directory,
        ))


def _get_file_row(md5_hash: str):
    stmt = select(files_table).where(files_table.c.md5_hash == md5_hash)
    with get_engine().connect() as conn:
        row = conn.execute(stmt).fetchone()
    return row._asdict() if row else None


def _get_path_conflicts(md5_hash: str = None):
    stmt = select(path_conflicts_table)
    if md5_hash:
        stmt = stmt.where(path_conflicts_table.c.md5_hash == md5_hash)
    with get_engine().connect() as conn:
        return [r._asdict() for r in conn.execute(stmt).fetchall()]


def test_apply_relink_keeps_keywords_location_and_list_membership(db, root_dir):
    md5_hash = 'h1'
    _insert_file_row('/old', 'f.jpg', md5_hash)

    with get_engine().begin() as conn:
        conn.execute(file_details_table.insert().values(md5_hash=md5_hash, description='desc'))
        conn.execute(keywords_table.insert().values(id=1, keyword='kw'))
        conn.execute(file_keywords_table.insert().values(md5_hash=md5_hash, keyword_id=1))
        conn.execute(lists_table.insert().values(id=1, name='mylist'))
        conn.execute(list_items_table.insert().values(list_id=1, md5_hash=md5_hash, item_code='ABC12345'))

    scan_results = [_sc(md5_hash, '/new', 'f.jpg')]
    classification = rediscover.classify(scan_results, {md5_hash: {'directory': '/old', 'file_name': 'f.jpg'}},
                                          exists=_no_exists)
    result = rediscover.apply(classification, scan_results, db, scanned_directory='/new',
                              track_new=False, exists=_no_exists)

    assert result.relinked == 1
    row = _get_file_row(md5_hash)
    assert row['directory'] == '/new'
    assert row['file_name'] == 'f.jpg'

    with get_engine().connect() as conn:
        assert conn.execute(select(file_details_table.c.description)
                            .where(file_details_table.c.md5_hash == md5_hash)).scalar() == 'desc'
        kw = conn.execute(select(keywords_table.c.keyword)
                          .join(file_keywords_table, keywords_table.c.id == file_keywords_table.c.keyword_id)
                          .where(file_keywords_table.c.md5_hash == md5_hash)).scalar()
        assert kw == 'kw'
        item_code = conn.execute(select(list_items_table.c.item_code)
                                 .where(list_items_table.c.md5_hash == md5_hash)).scalar()
        assert item_code == 'ABC12345'


def test_apply_persists_conflicts_without_duplicates_on_rerun(db, root_dir):
    md5_hash = 'h1'
    _insert_file_row('/a', 'f.jpg', md5_hash)
    scan_results = [_sc(md5_hash, '/a', 'f.jpg'), _sc(md5_hash, '/b', 'copy.jpg')]
    tracked = {md5_hash: {'directory': '/a', 'file_name': 'f.jpg'}}

    classification = rediscover.classify(scan_results, tracked, exists=_all_exist)
    rediscover.apply(classification, scan_results, db, scanned_directory='/a',
                     track_new=False, exists=_all_exist)

    conflicts = _get_path_conflicts(md5_hash)
    assert len(conflicts) == 1
    assert conflicts[0]['candidate_path'] == '/b/copy.jpg'
    assert conflicts[0]['source'] == 'rediscover'

    # Re-running the same rediscover must not create a duplicate row.
    classification2 = rediscover.classify(scan_results, tracked, exists=_all_exist)
    rediscover.apply(classification2, scan_results, db, scanned_directory='/a',
                     track_new=False, exists=_all_exist)
    assert len(_get_path_conflicts(md5_hash)) == 1


def test_apply_prunes_conflicts_whose_candidate_disappeared(db, root_dir, tmp_path):
    md5_hash = 'h1'
    tracked_path = tmp_path / 'f.jpg'
    tracked_path.write_bytes(b'x')
    _insert_file_row(str(tmp_path), 'f.jpg', md5_hash)

    candidate_path = tmp_path / 'copy.jpg'
    candidate_path.write_bytes(b'x')

    scan_results = [_sc(md5_hash, str(tmp_path), 'f.jpg'), _sc(md5_hash, str(tmp_path), 'copy.jpg')]
    tracked = {md5_hash: {'directory': str(tmp_path), 'file_name': 'f.jpg'}}
    real_exists = lambda p: Path(p).exists()

    classification = rediscover.classify(scan_results, tracked, exists=real_exists)
    rediscover.apply(classification, scan_results, db, scanned_directory=str(tmp_path),
                     track_new=False, exists=real_exists)
    assert len(_get_path_conflicts(md5_hash)) == 1

    # The extra copy disappears from disk; rediscovering again should prune it.
    candidate_path.unlink()
    scan_results2 = [_sc(md5_hash, str(tmp_path), 'f.jpg')]
    classification2 = rediscover.classify(scan_results2, tracked, exists=real_exists)
    result = rediscover.apply(classification2, scan_results2, db, scanned_directory=str(tmp_path),
                              track_new=False, exists=real_exists)

    assert result.pruned_conflicts == 1
    assert _get_path_conflicts(md5_hash) == []


def test_apply_track_new_tracks_first_sorted_path_and_conflicts_the_rest(db, root_dir):
    md5_hash = 'h1'
    scan_results = [_sc(md5_hash, '/b', 'f.jpg'), _sc(md5_hash, '/a', 'f.jpg')]
    classification = rediscover.classify(scan_results, tracked={}, exists=_no_exists)

    tracked_calls = []

    def track_new_files(to_track):
        tracked_calls.extend(to_track)
        db.insert_scan_results(to_track)

    # All "found" paths genuinely exist (the scanner only returns files that
    # do) — exists() here must agree, or the fresh conflict just inserted
    # would be immediately pruned as stale.
    result = rediscover.apply(classification, scan_results, db, scanned_directory='/a',
                              track_new=True, track_new_files=track_new_files, exists=_all_exist)

    assert result.new_tracked == 1
    assert len(tracked_calls) == 1
    assert tracked_calls[0].directory == '/a'  # sorted first

    row = _get_file_row(md5_hash)
    assert row is not None
    assert row['directory'] == '/a'

    conflicts = _get_path_conflicts(md5_hash)
    assert len(conflicts) == 1
    assert conflicts[0]['candidate_path'] == '/b/f.jpg'


def test_apply_track_new_false_only_counts(db, root_dir):
    md5_hash = 'h1'
    scan_results = [_sc(md5_hash, '/a', 'f.jpg')]
    classification = rediscover.classify(scan_results, tracked={}, exists=_no_exists)

    result = rediscover.apply(classification, scan_results, db, scanned_directory='/a',
                              track_new=False, exists=_no_exists)

    assert result.new_found == 1
    assert result.new_tracked == 0
    assert _get_file_row(md5_hash) is None


def test_apply_conflict_does_not_touch_metadata(db, root_dir):
    md5_hash = 'h1'
    _insert_file_row('/old', 'f.jpg', md5_hash)
    with get_engine().begin() as conn:
        conn.execute(file_details_table.insert().values(md5_hash=md5_hash, description='desc'))

    # Old path still exists on disk -> conflict, nothing changes.
    scan_results = [_sc(md5_hash, '/new', 'f.jpg')]
    tracked = {md5_hash: {'directory': '/old', 'file_name': 'f.jpg'}}
    classification = rediscover.classify(scan_results, tracked, exists=_all_exist)
    rediscover.apply(classification, scan_results, db, scanned_directory='/new',
                     track_new=False, exists=_all_exist)

    row = _get_file_row(md5_hash)
    assert row['directory'] == '/old'  # untouched
    with get_engine().connect() as conn:
        assert conn.execute(select(file_details_table.c.description)
                            .where(file_details_table.c.md5_hash == md5_hash)).scalar() == 'desc'
