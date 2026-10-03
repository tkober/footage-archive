"""Tests for the normal scan's reconciliation path (#26): `index_files_in_directory`
(POST /tracking/scan-directory) and `index_single_file` (POST /tracking/scan-file)
must reuse fileops/rediscover.py's classify()/apply() instead of silently moving a
known hash's path via the `Files` upsert. `_probe_and_save` is monkeypatched so
these tests never shell out to ffprobe/exiftool; it also records which ScanResults
were actually probed, so "not probed" assertions have teeth."""

from pathlib import Path

from sqlalchemy import select

import api.tracking as tracking
from api.dtos import FileQuery
from db.engine import get_engine
from db.models import file_details_table, file_keywords_table, files_table, keywords_table, path_conflicts_table


def _write(path: Path, content: bytes = b'x'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


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


class _ProbeRecorder:
    """Monkeypatch target for api.tracking._probe_and_save — records which
    (directory, file_name) got probed instead of touching ffprobe/exiftool/DB
    detail tables."""

    def __init__(self):
        self.probed: list[str] = []

    def __call__(self, sc, db, generate_clip_preview):
        self.probed.append(f'{sc.directory}/{sc.file_name}')


class _Report:
    def __init__(self):
        self.messages: list[str] = []

    def __call__(self, message: str):
        self.messages.append(message)

    @property
    def last(self) -> str:
        return self.messages[-1] if self.messages else ''


def _patch_probe(monkeypatch) -> _ProbeRecorder:
    recorder = _ProbeRecorder()
    monkeypatch.setattr(tracking, '_probe_and_save', recorder)
    return recorder


def test_scan_directory_copy_of_tracked_file_is_conflict_not_moved(db, root_dir, monkeypatch):
    recorder = _patch_probe(monkeypatch)

    original = root_dir / 'orig' / 'f.jpg'
    _write(original, b'same-bytes')
    copy = root_dir / 'other' / 'copy.jpg'
    _write(copy, b'same-bytes')

    scan_results = __import__('scanner.scanner', fromlist=['Scanner']).Scanner().scan_files([original])
    md5_hash = scan_results[0].md5_hash
    db.insert_scan_results(scan_results)

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir / 'other')), report)

    row = _get_file_row(md5_hash)
    assert row['directory'] == str(original.parent)  # unchanged — not stolen by the copy
    assert row['file_name'] == 'f.jpg'

    conflicts = _get_path_conflicts(md5_hash)
    assert len(conflicts) == 1
    assert conflicts[0]['candidate_path'] == str(copy)
    assert conflicts[0]['source'] == 'scan'

    assert str(copy) not in recorder.probed
    assert '1 conflicts' in report.last


def test_scan_directory_moved_tracked_file_relinks_and_probes_new_path(db, root_dir, monkeypatch):
    recorder = _patch_probe(monkeypatch)

    old_dir = root_dir / 'old'
    old_path = old_dir / 'f.jpg'
    _write(old_path)
    scan_results = __import__('scanner.scanner', fromlist=['Scanner']).Scanner().scan_files([old_path])
    md5_hash = scan_results[0].md5_hash
    db.insert_scan_results(scan_results)

    with get_engine().begin() as conn:
        conn.execute(file_details_table.insert().values(md5_hash=md5_hash, description='desc'))
        conn.execute(keywords_table.insert().values(id=1, keyword='kw'))
        conn.execute(file_keywords_table.insert().values(md5_hash=md5_hash, keyword_id=1))

    # Move it on disk: old path gone, found once at the new path.
    new_dir = root_dir / 'new'
    new_path = new_dir / 'f.jpg'
    new_dir.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    old_dir.rmdir()

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(new_dir)), report)

    row = _get_file_row(md5_hash)
    assert row['directory'] == str(new_dir)
    assert row['file_name'] == 'f.jpg'
    assert str(new_path) in recorder.probed

    with get_engine().connect() as conn:
        assert conn.execute(select(file_details_table.c.description)
                            .where(file_details_table.c.md5_hash == md5_hash)).scalar() == 'desc'
        kw = conn.execute(select(keywords_table.c.keyword)
                          .join(file_keywords_table, keywords_table.c.id == file_keywords_table.c.keyword_id)
                          .where(file_keywords_table.c.md5_hash == md5_hash)).scalar()
        assert kw == 'kw'

    assert '1 relinked' in report.last


def test_scan_directory_duplicate_unknown_file_tracks_one_conflicts_other(db, root_dir, monkeypatch):
    recorder = _patch_probe(monkeypatch)

    a = root_dir / 'a' / 'f.jpg'
    b = root_dir / 'b' / 'f.jpg'
    _write(a, b'dup-bytes')
    _write(b, b'dup-bytes')

    scanner_module = __import__('scanner.scanner', fromlist=['Scanner'])
    md5_hash = scanner_module.Scanner().scan_files([a])[0].md5_hash

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), report)

    row = _get_file_row(md5_hash)
    assert row is not None
    tracked_path = f"{row['directory']}/{row['file_name']}"
    assert tracked_path in (str(a), str(b))

    other_path = str(b) if tracked_path == str(a) else str(a)
    conflicts = _get_path_conflicts(md5_hash)
    assert len(conflicts) == 1
    assert conflicts[0]['candidate_path'] == other_path

    assert tracked_path in recorder.probed
    assert other_path not in recorder.probed


def test_scan_directory_unchanged_file_is_reprobed_new_file_inserted_and_probed(db, root_dir, monkeypatch):
    recorder = _patch_probe(monkeypatch)

    tracked_file = root_dir / 'f.jpg'
    _write(tracked_file)
    scanner_module = __import__('scanner.scanner', fromlist=['Scanner'])
    tracked_scan = scanner_module.Scanner().scan_files([tracked_file])
    tracked_hash = tracked_scan[0].md5_hash
    db.insert_scan_results(tracked_scan)

    new_file = root_dir / 'new.jpg'
    _write(new_file, b'brand-new')

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), report)

    assert str(tracked_file) in recorder.probed  # unchanged file re-probed

    new_hash = scanner_module.Scanner().scan_files([new_file])[0].md5_hash
    new_row = _get_file_row(new_hash)
    assert new_row is not None
    assert new_row['directory'] == str(root_dir)
    assert str(new_file) in recorder.probed  # new file inserted + probed

    assert 'Indexed 2 files' in report.last
    assert '0 relinked' in report.last
    assert '0 conflicts' in report.last


def test_scan_file_copy_of_tracked_file_is_conflict_no_path_flip(db, root_dir, monkeypatch):
    recorder = _patch_probe(monkeypatch)

    original = root_dir / 'orig' / 'f.jpg'
    _write(original, b'same-bytes')
    copy = root_dir / 'other' / 'copy.jpg'
    _write(copy, b'same-bytes')

    scanner_module = __import__('scanner.scanner', fromlist=['Scanner'])
    scan_results = scanner_module.Scanner().scan_files([original])
    md5_hash = scan_results[0].md5_hash
    db.insert_scan_results(scan_results)

    report = _Report()
    tracking.index_single_file(FileQuery(path=str(copy)), report)

    row = _get_file_row(md5_hash)
    assert row['directory'] == str(original.parent)
    assert row['file_name'] == 'f.jpg'

    conflicts = _get_path_conflicts(md5_hash)
    assert len(conflicts) == 1
    assert conflicts[0]['candidate_path'] == str(copy)
    assert conflicts[0]['source'] == 'scan'

    assert str(copy) not in recorder.probed
