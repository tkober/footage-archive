"""Tests for the normal scan's reconciliation path (#26): `index_files_in_directory`
(POST /tracking/scan-directory) and `index_single_file` (POST /tracking/scan-file)
must reuse fileops/rediscover.py's classify()/apply() instead of silently moving a
known hash's path via the `Files` upsert. `_probe_and_save` is monkeypatched so
these tests never shell out to ffprobe/exiftool; it also records which ScanResults
were actually probed, so "not probed" assertions have teeth.

The tests from `test_scan_directory_batching_matches_single_batch_size` onward
cover the streaming scan (#135): `index_files_in_directory` now hashes +
reconciles its candidates in batches of `SCAN_BATCH_SIZE` instead of over the
whole tree at once.

The tests from `test_scan_directory_second_scan_skips_unchanged_files` onward
cover the incremental scan (#136): a file already tracked at exactly its path
with an unchanged size+mtime signature is skipped — not hashed, not probed —
on a later scan of the same folder."""

import os
import re
from pathlib import Path

import pytest
from sqlalchemy import select, update

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


def _count_hash_calls(monkeypatch) -> list[str]:
    """Wrap Scanner.md5_hash (#136) to record every path it's actually
    called for, without changing its behaviour — the incremental scan's
    "hashes nothing" assertions need something with teeth, since a skipped
    candidate never even reaches hash_candidates()."""
    import scanner.scanner as scanner_module
    original = scanner_module.Scanner.md5_hash
    calls: list[str] = []

    def counting_md5_hash(self, path):
        calls.append(path)
        return original(self, path)

    monkeypatch.setattr(scanner_module.Scanner, 'md5_hash', counting_md5_hash)
    return calls


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


def _progress_values(messages: list[str], label: str) -> list[int]:
    """Extract every `{label} x / y` message's `x` as a list, in report order."""
    pattern = re.compile(rf'^{label} (\d+) / \d+')
    values = []
    for message in messages:
        match = pattern.match(message)
        if match:
            values.append(int(match.group(1)))
    return values


@pytest.mark.parametrize('batch_size', [2, 100])
def test_scan_directory_batching_matches_single_batch_size(db, root_dir, monkeypatch, batch_size):
    """With SCAN_BATCH_SIZE=2, two byte-identical NEW files (`b.jpg`/`d.jpg`)
    fall into different batches (candidates are sorted alphabetically before
    batching: a, b, c, d, e). The second batch only sees `d.jpg` in its own
    `scan_results`, but `b.jpg`'s hash is already tracked (inserted by the
    first batch) and still exists on disk, so classify() takes rule 3 (old
    path exists → conflict) — same outcome as a single SCAN_BATCH_SIZE=100
    batch, where both copies are classified together directly (rule 3 too):
    the alphabetically first copy (b) is tracked, the other (d) conflicts."""
    monkeypatch.setenv('SCAN_BATCH_SIZE', str(batch_size))
    recorder = _patch_probe(monkeypatch)

    paths = {}
    for name, content in [('a.jpg', b'a'), ('b.jpg', b'dup'), ('c.jpg', b'c'),
                           ('d.jpg', b'dup'), ('e.jpg', b'e')]:
        p = root_dir / name
        _write(p, content)
        paths[name] = p

    scanner_module = __import__('scanner.scanner', fromlist=['Scanner'])
    dup_hash = scanner_module.Scanner().scan_files([paths['b.jpg']])[0].md5_hash

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), report)

    row = _get_file_row(dup_hash)
    assert row is not None
    assert f"{row['directory']}/{row['file_name']}" == str(paths['b.jpg'])

    conflicts = _get_path_conflicts(dup_hash)
    assert len(conflicts) == 1
    assert conflicts[0]['candidate_path'] == str(paths['d.jpg'])
    assert len(_get_path_conflicts()) == 1  # no conflicts for a/c/e

    assert set(recorder.probed) == {
        str(paths['a.jpg']), str(paths['b.jpg']), str(paths['c.jpg']), str(paths['e.jpg']),
    }
    assert str(paths['d.jpg']) not in recorder.probed

    # The summary counts the duplicate as one conflict whichever way the
    # batches fall (new hash with a second copy, or rule-3 conflict).
    assert 'Indexed 4 files · 0 relinked · 1 conflicts' in report.last


def test_scan_directory_relink_then_conflict_split_across_batches_is_pinned(db, root_dir, monkeypatch):
    """Known, accepted difference from a single-batch scan (#135): a tracked
    hash whose old path is gone, found at two new copies that land in
    different batches (SCAN_BATCH_SIZE=1 here), relinks to the first batch's
    copy and records the second batch's copy as a conflict — instead of a
    conflict-without-relink the way today's single-batch scan classifies it
    (rule 5: old path gone, found more than once -> conflict, no relink).
    Both copies stay visible (one tracked, one on the conflicts page);
    nothing is lost. Pinned here so a later change notices if it drifts."""
    recorder = _patch_probe(monkeypatch)
    monkeypatch.setenv('SCAN_BATCH_SIZE', '1')

    old_dir = root_dir / 'old'
    old_path = old_dir / 'f.jpg'
    _write(old_path, b'moved-bytes')
    scanner_module = __import__('scanner.scanner', fromlist=['Scanner'])
    scan_results = scanner_module.Scanner().scan_files([old_path])
    md5_hash = scan_results[0].md5_hash
    db.insert_scan_results(scan_results)

    copy_a = root_dir / 'copy_a.jpg'
    copy_b = root_dir / 'copy_b.jpg'
    _write(copy_a, b'moved-bytes')
    _write(copy_b, b'moved-bytes')
    old_path.unlink()
    old_dir.rmdir()

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), report)

    row = _get_file_row(md5_hash)
    assert f"{row['directory']}/{row['file_name']}" == str(copy_a)  # first batch's copy wins the relink

    conflicts = _get_path_conflicts(md5_hash)
    assert len(conflicts) == 1
    assert conflicts[0]['candidate_path'] == str(copy_b)

    assert str(copy_a) in recorder.probed
    assert str(copy_b) not in recorder.probed

    assert 'Indexed 1 files · 1 relinked · 1 conflicts' in report.last


def test_scan_directory_progress_messages_monotonic_and_reach_total(db, root_dir, monkeypatch):
    """Hashed/Probed progress is one counter spanning the whole scan, not
    reset per batch: with 5 distinct (non-conflicting) files and
    SCAN_BATCH_SIZE=2 (three batches), both sequences must be non-decreasing
    and finish at `n` — `n` being the total candidate count, not just the
    per-batch size."""
    monkeypatch.setenv('SCAN_BATCH_SIZE', '2')
    _patch_probe(monkeypatch)

    for i, name in enumerate(['a.jpg', 'b.jpg', 'c.jpg', 'd.jpg', 'e.jpg']):
        _write(root_dir / name, bytes([i]))

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), report)

    hashed = _progress_values(report.messages, 'Hashed')
    probed = _progress_values(report.messages, 'Probed')

    assert hashed == sorted(hashed)
    assert probed == sorted(probed)
    assert hashed[-1] == 5
    assert probed[-1] == 5
    assert 'Indexed 5 files · 0 relinked · 0 conflicts' in report.last


def test_scan_directory_batch_hash_failure_does_not_abort_later_batches(db, root_dir, monkeypatch):
    """A file that can't be hashed (OSError — vanished, permission, I/O) is
    logged, counted as failed, and skipped; the rest of its batch and every
    later batch still run (#135). Today, a single unreadable file aborts the
    whole scan via `parallel_map` — this isolation is new for the streaming
    path only."""
    monkeypatch.setenv('SCAN_BATCH_SIZE', '2')
    recorder = _patch_probe(monkeypatch)

    good_a = root_dir / 'a.jpg'
    bad = root_dir / 'b.jpg'
    good_c = root_dir / 'c.jpg'
    _write(good_a, b'a')
    _write(bad, b'b')
    _write(good_c, b'c')

    scanner_module = __import__('scanner.scanner', fromlist=['Scanner'])
    original_md5_hash = scanner_module.Scanner.md5_hash

    def flaky_md5_hash(self, path):
        if path == str(bad):
            raise OSError('vanished mid-scan')
        return original_md5_hash(self, path)

    monkeypatch.setattr(scanner_module.Scanner, 'md5_hash', flaky_md5_hash)

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), report)

    assert str(good_a) in recorder.probed
    assert str(good_c) in recorder.probed
    assert str(bad) not in recorder.probed

    row_a = _get_file_row(scanner_module.Scanner().scan_files([good_a])[0].md5_hash)
    assert row_a is not None
    row_c = _get_file_row(scanner_module.Scanner().scan_files([good_c])[0].md5_hash)
    assert row_c is not None

    assert 'Indexed 2 files · 0 relinked · 0 conflicts · 1 failed' in report.last

    hashed = _progress_values(report.messages, 'Hashed')
    probed = _progress_values(report.messages, 'Probed')
    assert hashed[-1] == 3
    assert probed[-1] == 3


def test_scan_directory_second_scan_skips_unchanged_files(db, root_dir, monkeypatch):
    """A file already tracked at exactly its path, with its size+mtime
    signature written by the first scan's probe, is skipped by the second
    scan — not hashed, not probed (#136)."""
    recorder = _patch_probe(monkeypatch)
    hash_calls = _count_hash_calls(monkeypatch)

    _write(root_dir / 'a.jpg', b'a')
    _write(root_dir / 'b.jpg', b'b')

    first = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), first)
    assert len(hash_calls) == 2
    assert 'Indexed 2 files · 0 relinked · 0 conflicts' in first.last

    hash_calls.clear()
    recorder.probed.clear()

    second = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), second)

    assert hash_calls == []
    assert recorder.probed == []
    assert 'Indexed 0 files · 2 skipped unchanged · 0 relinked · 0 conflicts' in second.last


def test_scan_directory_changed_file_is_rehashed_others_skipped(db, root_dir, monkeypatch):
    """Only the file whose content+mtime actually changed is re-hashed and
    re-probed; its untouched sibling is skipped (#136). `os.utime` sets a
    distinct mtime_ns explicitly so the test isn't timing-dependent (two
    writes close together could otherwise land on the same mtime)."""
    recorder = _patch_probe(monkeypatch)
    hash_calls = _count_hash_calls(monkeypatch)

    a = root_dir / 'a.jpg'
    b = root_dir / 'b.jpg'
    _write(a, b'a')
    _write(b, b'b')

    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), _Report())
    hash_calls.clear()
    recorder.probed.clear()

    _write(b, b'changed-content')
    current = b.stat()
    os.utime(b, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000))

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), report)

    assert hash_calls == [str(b)]
    assert recorder.probed == [str(b)]
    assert 'Indexed 1 files · 1 skipped unchanged · 0 relinked · 0 conflicts' in report.last


def test_scan_directory_renamed_file_is_hashed_and_relinked(db, root_dir, monkeypatch):
    """A rename changes (directory, file_name) even though the file's
    content — and so its size+mtime — didn't change, so the skip rule's
    path-keyed lookup never matches it: it's still hashed and relinked like
    before #136."""
    recorder = _patch_probe(monkeypatch)
    hash_calls = _count_hash_calls(monkeypatch)

    old_dir = root_dir / 'old'
    old_path = old_dir / 'f.jpg'
    _write(old_path)

    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), _Report())
    hash_calls.clear()
    recorder.probed.clear()

    new_dir = root_dir / 'new'
    new_path = new_dir / 'f.jpg'
    new_dir.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    old_dir.rmdir()

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), report)

    assert hash_calls == [str(new_path)]
    assert recorder.probed == [str(new_path)]
    assert '1 relinked' in report.last


def test_scan_directory_force_rehash_hashes_everything(db, root_dir, monkeypatch):
    """`force_rehash=True` bypasses the skip rule entirely, even for a file
    that would otherwise be skipped as unchanged (#136)."""
    recorder = _patch_probe(monkeypatch)
    hash_calls = _count_hash_calls(monkeypatch)

    a = root_dir / 'a.jpg'
    _write(a, b'a')
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), _Report())
    hash_calls.clear()
    recorder.probed.clear()

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir), force_rehash=True), report)

    assert hash_calls == [str(a)]
    assert recorder.probed == [str(a)]
    # No "N skipped unchanged" segment at all when nothing was skipped.
    assert 'Indexed 1 files · 0 relinked · 0 conflicts' in report.last
    assert 'skipped' not in report.last


def test_scan_directory_null_signature_is_rehashed_and_filled(db, root_dir, monkeypatch):
    """A row whose signature is NULL (never probed since the migration, or a
    crash left it that way) is never treated as unchanged — it's hashed and
    probed, and the fresh signature is filled in by that probe (#136)."""
    recorder = _patch_probe(monkeypatch)
    hash_calls = _count_hash_calls(monkeypatch)

    a = root_dir / 'a.jpg'
    _write(a, b'a')
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), _Report())

    scanner_module = __import__('scanner.scanner', fromlist=['Scanner'])
    md5_hash = scanner_module.Scanner().scan_files([a])[0].md5_hash
    row = _get_file_row(md5_hash)
    assert row['size_bytes'] is not None
    assert row['mtime_ns'] is not None

    with get_engine().begin() as conn:
        conn.execute(
            update(files_table).where(files_table.c.md5_hash == md5_hash)
            .values(size_bytes=None, mtime_ns=None)
        )

    hash_calls.clear()
    recorder.probed.clear()

    report = _Report()
    tracking.index_files_in_directory(FileQuery(path=str(root_dir)), report)

    assert hash_calls == [str(a)]
    assert recorder.probed == [str(a)]

    row_after = _get_file_row(md5_hash)
    assert row_after['size_bytes'] == row['size_bytes']
    assert row_after['mtime_ns'] == row['mtime_ns']


def test_insert_scan_results_never_overwrites_a_stored_signature(db, root_dir, monkeypatch):
    """`insert_scan_results`'s pre-probe upsert must never touch
    size_bytes/mtime_ns — simulated here directly (no real probe step)
    since that's exactly the crash-safety invariant #136 relies on: a row
    that exists with a signature keeps it through a bare insert_scan_results
    call, even one that reports a different size/mtime for the same hash."""
    scanner_module = __import__('scanner.scanner', fromlist=['Scanner'])
    a = root_dir / 'a.jpg'
    _write(a, b'a')
    scan_result = scanner_module.Scanner().scan_files([a])[0]
    md5_hash = scan_result.md5_hash

    db.insert_scan_results([scan_result])
    row = _get_file_row(md5_hash)
    assert row['size_bytes'] is None  # never written by insert_scan_results itself
    assert row['mtime_ns'] is None

    db.set_file_signatures([{
        'md5_hash': md5_hash, 'directory': scan_result.directory, 'file_name': scan_result.file_name,
        'size_bytes': 12345, 'mtime_ns': 67890,
    }])

    # A second insert_scan_results call for the same hash (e.g. a re-probe
    # bumping last_indexed_at) must not clobber the signature just written.
    db.insert_scan_results([scan_result])
    row_after = _get_file_row(md5_hash)
    assert row_after['size_bytes'] == 12345
    assert row_after['mtime_ns'] == 67890
