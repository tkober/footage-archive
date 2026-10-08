"""POST /files/directory — counts (#46), subfolder file_count, kind filter +
pagination, hidden-extension exclusion, and unchanged default behaviour.
Mirrors tests/test_files_api.py's HTTP-level TestClient pattern."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from api.files import FilesApi
from db.database import Database
from db.engine import get_engine
from db.models import files_table, video_details_table


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(FilesApi)
    return TestClient(app)


def _insert_file_row(directory: str, file_name: str, md5_hash: str, media_type='photo'):
    with get_engine().begin() as conn:
        conn.execute(files_table.insert().values(
            md5_hash=md5_hash, file_name=file_name,
            file_extension=Path(file_name).suffix, media_type=media_type,
            directory=directory,
        ))


def _list_dir(client: TestClient, path, **kwargs):
    body = {'path': str(path)}
    body.update(kwargs)
    resp = client.post('/files/directory', json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _count_sql_statements(fn):
    """Run `fn()` and return how many statements were sent to Postgres
    (#134's "only one additional query per directory request" proof) via a
    `before_cursor_execute` listener on the shared engine."""
    engine = get_engine()
    counter = {'n': 0}

    def _listener(conn, cursor, statement, parameters, context, executemany):
        counter['n'] += 1

    event.listen(engine, 'before_cursor_execute', _listener)
    try:
        fn()
    finally:
        event.remove(engine, 'before_cursor_execute', _listener)
    return counter['n']


def test_default_listing_is_unchanged_and_carries_counts(db, root_dir):
    client = _make_client()

    (root_dir / 'sub').mkdir()
    (root_dir / 'video.mov').write_bytes(b'x')
    (root_dir / 'photo.jpg').write_bytes(b'x')
    (root_dir / 'untracked.txt').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'video.mov', 'h_video', media_type='video')
    _insert_file_row(str(root_dir), 'photo.jpg', 'h_photo', media_type='photo')
    # untracked.txt deliberately left untracked (no DB row)

    body = _list_dir(client, root_dir)

    assert body['total'] == 4  # sub + 3 files
    names = {e['name'] for e in body['items']}
    assert names == {'sub', 'video.mov', 'photo.jpg', 'untracked.txt'}

    assert body['counts'] == {
        'directories': 1, 'video': 1, 'photo': 1, 'untracked': 1,
        'extensions': {'.mov': 1, '.jpg': 1, '.txt': 1},
    }


def test_counts_are_independent_of_pagination(db, root_dir):
    client = _make_client()

    for i in range(5):
        name = f'video{i}.mov'
        (root_dir / name).write_bytes(b'x')
        _insert_file_row(str(root_dir), name, f'h{i}', media_type='video')

    body = _list_dir(client, root_dir, page=1, page_size=2)
    assert len(body['items']) == 2
    assert body['total'] == 5
    assert body['counts'] == {
        'directories': 0, 'video': 5, 'photo': 0, 'untracked': 0,
        'extensions': {'.mov': 5},
    }


def test_360_variants_count_as_video_and_photo(db, root_dir):
    client = _make_client()

    (root_dir / 'a.insv').write_bytes(b'x')
    (root_dir / 'b.insp').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'a.insv', 'h1', media_type='360_video')
    _insert_file_row(str(root_dir), 'b.insp', 'h2', media_type='360_photo')

    body = _list_dir(client, root_dir)
    assert body['counts'] == {
        'directories': 0, 'video': 1, 'photo': 1, 'untracked': 0,
        'extensions': {'.insv': 1, '.insp': 1},
    }


def test_hidden_extensions_excluded_from_listing_and_counts(db, root_dir):
    client = _make_client()

    (root_dir / 'photo.jpg').write_bytes(b'x')
    (root_dir / 'photo.xmp').write_bytes(b'x')  # BROWSER_HIDDEN_EXTENSIONS default
    _insert_file_row(str(root_dir), 'photo.jpg', 'h1', media_type='photo')

    body = _list_dir(client, root_dir)
    names = {e['name'] for e in body['items']}
    assert names == {'photo.jpg'}
    assert body['counts'] == {
        'directories': 0, 'video': 0, 'photo': 1, 'untracked': 0,
        'extensions': {'.jpg': 1},
    }


def test_file_count_on_directory_entries(db, root_dir):
    client = _make_client()

    sub = root_dir / 'sub'
    sub.mkdir()
    (sub / 'a.jpg').write_bytes(b'x')
    (sub / 'b.jpg').write_bytes(b'x')
    (sub / 'hidden.xmp').write_bytes(b'x')  # excluded from file_count
    (sub / 'nested').mkdir()  # directories don't count towards file_count
    (sub / 'nested' / 'deep.jpg').write_bytes(b'x')  # not recursive

    empty_sub = root_dir / 'empty'
    empty_sub.mkdir()

    body = _list_dir(client, root_dir)
    by_name = {e['name']: e for e in body['items']}
    assert by_name['sub']['file_count'] == 2
    assert by_name['empty']['file_count'] == 0

    # file entries don't carry a file_count
    (root_dir / 'photo.jpg').write_bytes(b'x')
    body = _list_dir(client, root_dir)
    by_name = {e['name']: e for e in body['items']}
    assert by_name['photo.jpg']['file_count'] is None


def test_file_count_is_null_when_subdirectory_cannot_be_read(db, root_dir):
    client = _make_client()

    sub = root_dir / 'locked'
    sub.mkdir()
    (sub / 'a.jpg').write_bytes(b'x')
    sub.chmod(0o000)
    try:
        body = _list_dir(client, root_dir)
        by_name = {e['name']: e for e in body['items']}
        assert by_name['locked']['file_count'] is None
    finally:
        sub.chmod(0o755)  # restore so tmp_path cleanup can remove it


def test_kind_filter_video(db, root_dir):
    client = _make_client()

    (root_dir / 'sub').mkdir()
    (root_dir / 'v.mov').write_bytes(b'x')
    (root_dir / 'p.jpg').write_bytes(b'x')
    (root_dir / 'u.txt').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'v.mov', 'h1', media_type='video')
    _insert_file_row(str(root_dir), 'p.jpg', 'h2', media_type='photo')

    body = _list_dir(client, root_dir, kind='video')
    assert body['total'] == 1
    assert [e['name'] for e in body['items']] == ['v.mov']
    # directories/video/photo/untracked still describe the whole (unfiltered) directory
    assert body['counts']['directories'] == 1
    assert body['counts']['video'] == 1
    assert body['counts']['photo'] == 1
    assert body['counts']['untracked'] == 1
    # ...but extensions is scoped by `kind` (here: video only) — see DirectoryCounts docstring
    assert body['counts']['extensions'] == {'.mov': 1}


def test_kind_filter_photo(db, root_dir):
    client = _make_client()

    (root_dir / 'v.mov').write_bytes(b'x')
    (root_dir / 'p1.jpg').write_bytes(b'x')
    (root_dir / 'p2.jpg').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'v.mov', 'h1', media_type='video')
    _insert_file_row(str(root_dir), 'p1.jpg', 'h2', media_type='photo')
    _insert_file_row(str(root_dir), 'p2.jpg', 'h3', media_type='photo')

    body = _list_dir(client, root_dir, kind='photo')
    assert body['total'] == 2
    assert {e['name'] for e in body['items']} == {'p1.jpg', 'p2.jpg'}


def test_kind_filter_untracked(db, root_dir):
    client = _make_client()

    (root_dir / 'v.mov').write_bytes(b'x')
    (root_dir / 'u1.txt').write_bytes(b'x')
    (root_dir / 'u2.txt').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'v.mov', 'h1', media_type='video')
    # u1/u2 left untracked

    body = _list_dir(client, root_dir, kind='untracked')
    assert body['total'] == 2
    assert {e['name'] for e in body['items']} == {'u1.txt', 'u2.txt'}


def test_kind_filter_excludes_directories(db, root_dir):
    client = _make_client()

    (root_dir / 'sub').mkdir()
    (root_dir / 'u.txt').write_bytes(b'x')

    body = _list_dir(client, root_dir, kind='untracked')
    assert [e['name'] for e in body['items']] == ['u.txt']
    assert all(e['type'] == 'file' for e in body['items'])


def test_kind_filter_pagination_refers_to_filtered_list(db, root_dir):
    client = _make_client()

    for i in range(5):
        name = f'p{i}.jpg'
        (root_dir / name).write_bytes(b'x')
        _insert_file_row(str(root_dir), name, f'h{i}', media_type='photo')
    # noise that must not be counted in the filtered pagination
    (root_dir / 'v.mov').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'v.mov', 'hv', media_type='video')
    (root_dir / 'sub').mkdir()

    page1 = _list_dir(client, root_dir, kind='photo', page=1, page_size=2)
    assert page1['total'] == 5
    assert len(page1['items']) == 2

    page3 = _list_dir(client, root_dir, kind='photo', page=3, page_size=2)
    assert len(page3['items']) == 1  # 5 photos, page_size 2 -> last page has 1


def test_duration_tc_populated_for_tracked_video_only(db, root_dir):
    client = _make_client()

    (root_dir / 'video.mov').write_bytes(b'x')
    (root_dir / 'photo.jpg').write_bytes(b'x')
    (root_dir / 'untracked.txt').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'video.mov', 'h_video', media_type='video')
    _insert_file_row(str(root_dir), 'photo.jpg', 'h_photo', media_type='photo')
    # untracked.txt deliberately left untracked (no DB row)

    with get_engine().begin() as conn:
        conn.execute(video_details_table.insert().values(
            md5_hash='h_video', duration_tc='00:12:34:10',
        ))

    body = _list_dir(client, root_dir)
    by_name = {e['name']: e for e in body['items']}

    assert by_name['video.mov']['duration_tc'] == '00:12:34:10'
    assert by_name['photo.jpg']['duration_tc'] is None
    assert by_name['untracked.txt']['duration_tc'] is None


def test_no_kind_means_everything_as_today(db, root_dir):
    client = _make_client()

    (root_dir / 'sub').mkdir()
    (root_dir / 'v.mov').write_bytes(b'x')
    (root_dir / 'p.jpg').write_bytes(b'x')
    (root_dir / 'u.txt').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'v.mov', 'h1', media_type='video')
    _insert_file_row(str(root_dir), 'p.jpg', 'h2', media_type='photo')

    body = _list_dir(client, root_dir)
    assert body['total'] == 4
    assert {e['type'] for e in body['items']} == {'directory', 'file'}


def test_extension_filter_alone(db, root_dir):
    client = _make_client()

    (root_dir / 'sub').mkdir()
    (root_dir / 'a.rw2').write_bytes(b'x')
    (root_dir / 'b.rw2').write_bytes(b'x')
    (root_dir / 'c.jpg').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'a.rw2', 'h1', media_type='photo')
    _insert_file_row(str(root_dir), 'b.rw2', 'h2', media_type='photo')
    _insert_file_row(str(root_dir), 'c.jpg', 'h3', media_type='photo')

    body = _list_dir(client, root_dir, extension='.rw2')
    assert body['total'] == 2
    assert {e['name'] for e in body['items']} == {'a.rw2', 'b.rw2'}
    # directories are dropped once an extension filter is set
    assert all(e['type'] == 'file' for e in body['items'])


def test_extension_filter_combined_with_kind(db, root_dir):
    client = _make_client()

    (root_dir / 'a.jpg').write_bytes(b'x')  # photo, wrong extension
    (root_dir / 'b.mp4').write_bytes(b'x')  # video, right extension
    (root_dir / 'c.mp4').write_bytes(b'x')  # untracked, right extension
    _insert_file_row(str(root_dir), 'a.jpg', 'h1', media_type='photo')
    _insert_file_row(str(root_dir), 'b.mp4', 'h2', media_type='video')
    # c.mp4 left untracked

    body = _list_dir(client, root_dir, kind='video', extension='.mp4')
    assert body['total'] == 1
    assert [e['name'] for e in body['items']] == ['b.mp4']


@pytest.mark.parametrize('raw_extension', ['.RW2', 'rw2', 'RW2', '.rw2'])
def test_extension_filter_is_normalised(db, root_dir, raw_extension):
    client = _make_client()

    (root_dir / 'a.rw2').write_bytes(b'x')
    (root_dir / 'b.jpg').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'a.rw2', 'h1', media_type='photo')
    _insert_file_row(str(root_dir), 'b.jpg', 'h2', media_type='photo')

    body = _list_dir(client, root_dir, extension=raw_extension)
    assert [e['name'] for e in body['items']] == ['a.rw2']


def test_extensions_count_respects_kind_but_not_extension_filter(db, root_dir):
    client = _make_client()

    (root_dir / 'a.rw2').write_bytes(b'x')
    (root_dir / 'b.jpg').write_bytes(b'x')
    (root_dir / 'c.mov').write_bytes(b'x')  # different kind — must not appear
    _insert_file_row(str(root_dir), 'a.rw2', 'h1', media_type='photo')
    _insert_file_row(str(root_dir), 'b.jpg', 'h2', media_type='photo')
    _insert_file_row(str(root_dir), 'c.mov', 'h3', media_type='video')

    # Scoped by kind='photo'...
    body = _list_dir(client, root_dir, kind='photo')
    assert body['counts']['extensions'] == {'.rw2': 1, '.jpg': 1}

    # ...and the counts don't collapse once an extension is also chosen.
    body = _list_dir(client, root_dir, kind='photo', extension='.rw2')
    assert body['total'] == 1
    assert body['counts']['extensions'] == {'.rw2': 1, '.jpg': 1}


def test_files_without_extension_skipped_from_extensions_count(db, root_dir):
    client = _make_client()

    (root_dir / 'noext').write_bytes(b'x')
    (root_dir / 'a.jpg').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'a.jpg', 'h1', media_type='photo')
    # noext left untracked, media_type None, file_extension None

    body = _list_dir(client, root_dir)
    assert body['counts']['extensions'] == {'.jpg': 1}


def test_trash_dir_is_hidden_from_directory_listing(db, root_dir):
    client = _make_client()

    (root_dir / '.trash').mkdir()
    (root_dir / '.trash-old').mkdir()  # must stay visible — not the real trash
    (root_dir / 'photo.jpg').write_bytes(b'x')

    body = _list_dir(client, root_dir)

    names = {e['name'] for e in body['items']}
    assert '.trash' not in names
    assert '.trash-old' in names
    assert 'photo.jpg' in names


# ---------------------------------------------------------------------------
# System files hidden from the browser (#81): .DS_Store, Thumbs.db,
# desktop.ini, Insta360's fileinfo_list.list, AppleDouble ._* sidecars.
# ---------------------------------------------------------------------------

def test_system_files_excluded_from_listing_and_counts(db, root_dir):
    client = _make_client()

    (root_dir / 'photo.jpg').write_bytes(b'x')
    (root_dir / '.DS_Store').write_bytes(b'x')
    (root_dir / 'Thumbs.db').write_bytes(b'x')
    (root_dir / 'THUMBS.DB').write_bytes(b'x')  # case-insensitive match — same name as above on a
                                                 # case-sensitive filesystem these coexist, that's fine
    (root_dir / 'desktop.ini').write_bytes(b'x')
    (root_dir / 'fileinfo_list.list').write_bytes(b'x')
    (root_dir / '._photo.jpg').write_bytes(b'x')
    (root_dir / 'a_real_untracked_file.txt').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'photo.jpg', 'h1', media_type='photo')

    body = _list_dir(client, root_dir)
    names = {e['name'] for e in body['items']}
    assert names == {'photo.jpg', 'a_real_untracked_file.txt'}
    assert body['counts'] == {
        'directories': 0, 'video': 0, 'photo': 1, 'untracked': 1,
        'extensions': {'.jpg': 1, '.txt': 1},
    }


def test_file_count_excludes_system_files(db, root_dir):
    client = _make_client()

    sub = root_dir / 'sub'
    sub.mkdir()
    (sub / 'a.jpg').write_bytes(b'x')
    (sub / '.DS_Store').write_bytes(b'x')
    (sub / 'Thumbs.db').write_bytes(b'x')
    (sub / 'desktop.ini').write_bytes(b'x')
    (sub / 'fileinfo_list.list').write_bytes(b'x')
    (sub / '._a.jpg').write_bytes(b'x')

    only_system_files = root_dir / 'Camera01'
    only_system_files.mkdir()
    (only_system_files / '.DS_Store').write_bytes(b'x')

    body = _list_dir(client, root_dir)
    by_name = {e['name']: e for e in body['items']}
    assert by_name['sub']['file_count'] == 1
    assert by_name['Camera01']['file_count'] == 0


def test_browser_hidden_names_env_override(db, root_dir, monkeypatch):
    monkeypatch.setenv('BROWSER_HIDDEN_NAMES', 'custom_junk.dat')
    client = _make_client()

    (root_dir / 'custom_junk.dat').write_bytes(b'x')
    (root_dir / '.DS_Store').write_bytes(b'x')  # no longer hidden — override replaces the default

    body = _list_dir(client, root_dir)
    names = {e['name'] for e in body['items']}
    assert names == {'.DS_Store'}


# ---------------------------------------------------------------------------
# Untracked badge (#134): media_file_count / tracked_file_count /
# untracked_file_count on directory PathChild entries — direct level only,
# recursion is a later ticket.
# ---------------------------------------------------------------------------

def test_untracked_badge_counts_on_directory_entries(db, root_dir):
    client = _make_client()

    trip = root_dir / 'trip'
    trip.mkdir()
    (trip / 'a.jpg').write_bytes(b'x')
    (trip / 'b.jpg').write_bytes(b'x')
    (trip / 'c.jpg').write_bytes(b'x')  # relevant, left untracked
    (trip / 'notes.xmp').write_bytes(b'x')  # sidecar — hidden, counts nowhere
    (trip / 'readme.txt').write_bytes(b'x')  # counted in file_count, not media_file_count
    _insert_file_row(str(trip), 'a.jpg', 'h1', media_type='photo')
    _insert_file_row(str(trip), 'b.jpg', 'h2', media_type='photo')
    # c.jpg deliberately left untracked

    body = _list_dir(client, root_dir)
    by_name = {e['name']: e for e in body['items']}
    entry = by_name['trip']

    assert entry['file_count'] == 4  # a/b/c.jpg + readme.txt (xmp hidden)
    assert entry['media_file_count'] == 3
    assert entry['tracked_file_count'] == 2
    assert entry['untracked_file_count'] == 1


def test_untracked_badge_counts_zero_without_relevant_files(db, root_dir):
    client = _make_client()

    docs = root_dir / 'docs'
    docs.mkdir()
    (docs / 'readme.txt').write_bytes(b'x')
    (docs / 'notes.xmp').write_bytes(b'x')

    body = _list_dir(client, root_dir)
    by_name = {e['name']: e for e in body['items']}
    entry = by_name['docs']

    assert entry['media_file_count'] == 0
    assert entry['tracked_file_count'] == 0
    assert entry['untracked_file_count'] == 0


def test_untracked_badge_counts_are_none_for_file_entries(db, root_dir):
    client = _make_client()

    (root_dir / 'photo.jpg').write_bytes(b'x')

    body = _list_dir(client, root_dir)
    by_name = {e['name']: e for e in body['items']}
    entry = by_name['photo.jpg']

    assert entry['media_file_count'] is None
    assert entry['tracked_file_count'] is None
    assert entry['untracked_file_count'] is None


def test_untracked_badge_counts_are_none_when_subdirectory_cannot_be_read(db, root_dir):
    client = _make_client()

    locked = root_dir / 'locked'
    locked.mkdir()
    (locked / 'a.jpg').write_bytes(b'x')
    locked.chmod(0o000)
    try:
        body = _list_dir(client, root_dir)
        by_name = {e['name']: e for e in body['items']}
        entry = by_name['locked']
        assert entry['media_file_count'] is None
        assert entry['tracked_file_count'] is None
        assert entry['untracked_file_count'] is None
    finally:
        locked.chmod(0o755)  # restore so tmp_path cleanup can remove it


def test_directory_request_issues_constant_query_count_regardless_of_child_folders(db, root_dir):
    """Proves #134's tracked-count lookup is one query per request, not one
    per child folder: a folder with one child issues the same number of SQL
    statements as a folder with several."""
    client = _make_client()

    few = root_dir / 'few'
    few.mkdir()
    (few / 'only_child').mkdir()

    many = root_dir / 'many'
    many.mkdir()
    for i in range(6):
        (many / f'sub{i}').mkdir()

    few_count = _count_sql_statements(lambda: _list_dir(client, few))
    many_count = _count_sql_statements(lambda: _list_dir(client, many))

    assert few_count == many_count


def test_count_tracked_files_by_directory_called_once_per_request(db, root_dir, monkeypatch):
    client = _make_client()

    for i in range(4):
        (root_dir / f'sub{i}').mkdir()

    calls = []
    original = Database.count_tracked_files_by_directory

    def _spy(self, directories):
        calls.append(list(directories))
        return original(self, directories)

    monkeypatch.setattr(Database, 'count_tracked_files_by_directory', _spy)

    _list_dir(client, root_dir)

    assert len(calls) == 1
    assert len(calls[0]) == 4  # every child folder, in one call — never per child


# ---------------------------------------------------------------------------
# Database.count_tracked_files_by_directory (#134) — unit-level
# ---------------------------------------------------------------------------

def test_count_tracked_files_by_directory_empty_list_issues_no_query(db):
    counter = {'n': 0}

    def _call():
        result = db.count_tracked_files_by_directory([])
        assert result == {}

    assert _count_sql_statements(_call) == 0


def test_get_directory_stats_batch_called_once_per_request(db, root_dir, monkeypatch):
    """#139's reader extension: one extra query for every child folder's
    DirectoryStats row, same "once per request, never once per child"
    convention as count_tracked_files_by_directory above."""
    client = _make_client()
    from db.database import Database

    for i in range(4):
        (root_dir / f'sub{i}').mkdir()

    calls = []
    original = Database.get_directory_stats_batch

    def _spy(self, directories):
        calls.append(list(directories))
        return original(self, directories)

    monkeypatch.setattr(Database, 'get_directory_stats_batch', _spy)

    _list_dir(client, root_dir)

    assert len(calls) == 1
    assert len(calls[0]) == 4


def test_directory_request_issues_one_more_statement_for_directory_stats(db, root_dir):
    """#134's "constant query count" proof (a folder with one child issues
    the same number of statements as one with several) still holds with
    #139's extra DirectoryStats query added — it's one more query per
    request, not one per child, so it doesn't break that invariant."""
    client = _make_client()

    few = root_dir / 'few'
    few.mkdir()
    (few / 'only_child').mkdir()

    many = root_dir / 'many'
    many.mkdir()
    for i in range(6):
        (many / f'sub{i}').mkdir()

    few_count = _count_sql_statements(lambda: _list_dir(client, few))
    many_count = _count_sql_statements(lambda: _list_dir(client, many))

    assert few_count == many_count


def test_count_tracked_files_by_directory_missing_dirs_absent(db):
    _insert_file_row('/root/a', 'x.jpg', 'h1')
    _insert_file_row('/root/a', 'y.jpg', 'h2')
    _insert_file_row('/root/b', 'z.jpg', 'h3')

    result = db.count_tracked_files_by_directory(['/root/a', '/root/missing'])

    assert result == {'/root/a': 2}
    assert '/root/missing' not in result
    assert '/root/b' not in result  # not asked for, must not leak in
