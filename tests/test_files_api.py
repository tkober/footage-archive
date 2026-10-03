"""One HTTP-level smoke pass over the new fileops endpoints via FastAPI's
TestClient — exercising the router wiring + exception-to-HTTPException
mapping once, on top of the already-thorough fileops/service.py unit tests."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.files import FilesApi
from db.engine import get_engine
from db.models import files_table


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


def test_mkdir_then_move_then_rename_round_trip(db, root_dir):
    client = _make_client()

    resp = client.post('/files/mkdir', json={'parent': str(root_dir), 'name': 'target'})
    assert resp.status_code == 201
    target_path = resp.json()['path']
    assert target_path == str(root_dir / 'target')

    # mkdir on an existing name -> 409
    resp = client.post('/files/mkdir', json={'parent': str(root_dir), 'name': 'target'})
    assert resp.status_code == 409

    src = root_dir / 'photo.jpg'
    src.write_bytes(b'x')
    _insert_file_row(str(root_dir), 'photo.jpg', 'h1')

    preview = client.post('/files/move/preview', json={
        'paths': [str(src)], 'target_directory': target_path,
    })
    assert preview.status_code == 200
    body = preview.json()
    assert body['file_count'] == 1
    assert body['tracked_count'] == 1

    moved = client.post('/files/move', json={
        'paths': [str(src)], 'target_directory': target_path,
    })
    assert moved.status_code == 200
    results = moved.json()
    assert len(results) == 1
    assert results[0]['ok'] is True
    new_path = results[0]['new_path']
    assert new_path == str(root_dir / 'target' / 'photo.jpg')
    assert Path(new_path).exists()
    assert not src.exists()

    renamed = client.patch('/files/rename', json={'path': new_path, 'new_name': 'renamed.jpg'})
    assert renamed.status_code == 200
    renamed_body = renamed.json()
    assert renamed_body['path'] == str(root_dir / 'target' / 'renamed.jpg')
    assert renamed_body['tracked'] is True
    assert renamed_body['md5_hash'] == 'h1'


def test_rename_outside_root_dir_is_403(db, root_dir):
    client = _make_client()
    resp = client.patch('/files/rename', json={'path': '/etc/passwd', 'new_name': 'x'})
    assert resp.status_code == 403


def test_rename_missing_source_is_404(db, root_dir):
    client = _make_client()
    resp = client.patch('/files/rename', json={
        'path': str(root_dir / 'does_not_exist.jpg'), 'new_name': 'x.jpg',
    })
    assert resp.status_code == 404


def test_rename_directory_via_api(db, root_dir):
    client = _make_client()
    src_dir = root_dir / 'folder'
    src_dir.mkdir()

    resp = client.patch('/files/rename', json={'path': str(src_dir), 'new_name': 'renamed_folder'})
    assert resp.status_code == 200
    body = resp.json()
    assert body['path'] == str(root_dir / 'renamed_folder')
    assert body['is_directory'] is True
    assert body['tracked'] is False
    assert not src_dir.exists()
    assert (root_dir / 'renamed_folder').exists()
