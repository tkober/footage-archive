"""POST /files/directory — counts (#46), subfolder file_count, kind filter +
pagination, hidden-extension exclusion, and unchanged default behaviour.
Mirrors tests/test_files_api.py's HTTP-level TestClient pattern."""

from pathlib import Path

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

    assert body['counts'] == {'directories': 1, 'video': 1, 'photo': 1, 'untracked': 1}


def test_counts_are_independent_of_pagination(db, root_dir):
    client = _make_client()

    for i in range(5):
        name = f'video{i}.mov'
        (root_dir / name).write_bytes(b'x')
        _insert_file_row(str(root_dir), name, f'h{i}', media_type='video')

    body = _list_dir(client, root_dir, page=1, page_size=2)
    assert len(body['items']) == 2
    assert body['total'] == 5
    assert body['counts'] == {'directories': 0, 'video': 5, 'photo': 0, 'untracked': 0}


def test_360_variants_count_as_video_and_photo(db, root_dir):
    client = _make_client()

    (root_dir / 'a.insv').write_bytes(b'x')
    (root_dir / 'b.insp').write_bytes(b'x')
    _insert_file_row(str(root_dir), 'a.insv', 'h1', media_type='360_video')
    _insert_file_row(str(root_dir), 'b.insp', 'h2', media_type='360_photo')

    body = _list_dir(client, root_dir)
    assert body['counts'] == {'directories': 0, 'video': 1, 'photo': 1, 'untracked': 0}


def test_hidden_extensions_excluded_from_listing_and_counts(db, root_dir):
    client = _make_client()

    (root_dir / 'photo.jpg').write_bytes(b'x')
    (root_dir / 'photo.xmp').write_bytes(b'x')  # BROWSER_HIDDEN_EXTENSIONS default
    _insert_file_row(str(root_dir), 'photo.jpg', 'h1', media_type='photo')

    body = _list_dir(client, root_dir)
    names = {e['name'] for e in body['items']}
    assert names == {'photo.jpg'}
    assert body['counts'] == {'directories': 0, 'video': 0, 'photo': 1, 'untracked': 0}


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
    # counts still describe the whole (unfiltered) directory
    assert body['counts'] == {'directories': 1, 'video': 1, 'photo': 1, 'untracked': 1}


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
