"""POST /files/directory — counts (#46), subfolder file_count, kind filter +
pagination, hidden-extension exclusion, and unchanged default behaviour.
Mirrors tests/test_files_api.py's HTTP-level TestClient pattern."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.files import FilesApi
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
