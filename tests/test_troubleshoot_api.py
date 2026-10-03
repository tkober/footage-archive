"""HTTP-level smoke tests for GET /trouble-shooting/missing-files, mirroring
tests/test_files_api.py's TestClient pattern."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.troubleshoot import TroubleShootingApi
from db.engine import get_engine
from db.models import (
    file_details_table,
    file_keywords_table,
    files_table,
    keywords_table,
    list_items_table,
    lists_table,
    locations_table,
)


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(TroubleShootingApi)
    return TestClient(app)


def _insert_file_row(directory: str, file_name: str, md5_hash: str, media_type='photo'):
    with get_engine().begin() as conn:
        conn.execute(files_table.insert().values(
            md5_hash=md5_hash, file_name=file_name,
            file_extension=Path(file_name).suffix, media_type=media_type,
            directory=directory,
        ))


def test_file_that_exists_on_disk_is_not_reported(db, root_dir):
    client = _make_client()
    existing = root_dir / 'present.jpg'
    existing.write_bytes(b'x')
    _insert_file_row(str(root_dir), 'present.jpg', 'h1')

    resp = client.get('/trouble-shooting/missing-files')
    assert resp.status_code == 200
    assert resp.json() == []


def test_missing_file_is_reported_with_attachment_counts(db, root_dir):
    client = _make_client()
    md5 = 'missinghash'
    _insert_file_row(str(root_dir), 'gone.mov', md5, media_type='video')
    # No file written to disk at all -> directory/gone.mov does not exist.

    with get_engine().begin() as conn:
        conn.execute(locations_table.insert().values(id=1, name='Kyoto'))
        conn.execute(file_details_table.insert().values(md5_hash=md5, location_id=1))
        conn.execute(keywords_table.insert().values(id=1, keyword='sunset'))
        conn.execute(keywords_table.insert().values(id=2, keyword='beach'))
        conn.execute(file_keywords_table.insert().values(md5_hash=md5, keyword_id=1))
        conn.execute(file_keywords_table.insert().values(md5_hash=md5, keyword_id=2))
        conn.execute(lists_table.insert().values(id=1, name='Favorites'))
        conn.execute(list_items_table.insert().values(list_id=1, md5_hash=md5, item_code='ABC123'))

    resp = client.get('/trouble-shooting/missing-files')
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item['md5_hash'] == md5
    assert item['file_name'] == 'gone.mov'
    assert item['directory'] == str(root_dir)
    assert item['media_type'] == 'video'
    assert item['keyword_count'] == 2
    assert item['has_location'] is True
    assert item['list_count'] == 1
    assert item['has_preview'] is False


def test_path_filter_limits_to_subtree_without_matching_sibling_prefix(db, root_dir):
    client = _make_client()
    sub_a = root_dir / 'a'
    sub_a.mkdir()
    sub_a_b = sub_a / 'b'
    sub_a_b.mkdir()
    sub_a_bc = sub_a / 'bc'
    sub_a_bc.mkdir()

    _insert_file_row(str(sub_a_b), 'missing_in_b.jpg', 'h_b')
    _insert_file_row(str(sub_a_bc), 'missing_in_bc.jpg', 'h_bc')

    # Filtering by /a/b must not also match the sibling /a/bc.
    resp = client.get('/trouble-shooting/missing-files', params={'path': str(sub_a_b)})
    assert resp.status_code == 200
    body = resp.json()
    assert [item['md5_hash'] for item in body] == ['h_b']


def test_path_outside_root_dir_is_403(db, root_dir):
    client = _make_client()
    resp = client.get('/trouble-shooting/missing-files', params={'path': '/etc'})
    assert resp.status_code == 403
