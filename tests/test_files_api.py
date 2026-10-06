"""One HTTP-level smoke pass over the new fileops endpoints via FastAPI's
TestClient — exercising the router wiring + exception-to-HTTPException
mapping once, on top of the already-thorough fileops/service.py unit tests."""

import asyncio
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.files as files_api
from api.config import ConfigApi
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


def test_delete_preview_then_delete_via_api(db, root_dir):
    client = _make_client()

    src = root_dir / 'photo.jpg'
    src.write_bytes(b'x')
    _insert_file_row(str(root_dir), 'photo.jpg', 'h1')

    preview = client.post('/files/delete/preview', json={'paths': [str(src)]})
    assert preview.status_code == 200
    body = preview.json()
    assert body['file_count'] == 1
    assert body['tracked_count'] == 1
    assert body['list_item_count'] == 0
    assert body['keyword_count'] == 0

    deleted = client.post('/files/delete', json={'paths': [str(src)]})
    assert deleted.status_code == 200
    body = deleted.json()
    assert len(body['results']) == 1
    item = body['results'][0]
    assert item['ok'] is True
    assert item['trash_path'] == str(Path(body['trash_batch']) / 'photo.jpg')
    assert Path(item['trash_path']).exists()
    assert not src.exists()


def test_delete_root_dir_itself_via_api_reports_error_not_500(db, root_dir):
    client = _make_client()
    resp = client.post('/files/delete', json={'paths': [str(root_dir)]})
    assert resp.status_code == 200
    body = resp.json()
    assert body['results'][0]['ok'] is False


def test_move_into_trash_is_rejected_via_api(db, root_dir):
    client = _make_client()
    trash_dir = root_dir / '.trash'
    trash_dir.mkdir()
    src = root_dir / 'a.jpg'
    src.write_bytes(b'x')

    resp = client.post('/files/move', json={'paths': [str(src)], 'target_directory': str(trash_dir)})
    assert resp.status_code == 400


def test_full_image_render_does_not_block_other_requests(db, root_dir, monkeypatch):
    """Regression test for #92: a slow full-resolution RAW render used to run
    directly on the event loop (get_full_image was `async def`), freezing
    every other request for the duration. It must now run in the threadpool,
    so a concurrent request (e.g. file details) completes quickly instead of
    waiting for the render.

    Uses httpx's ASGI transport + asyncio.gather (rather than the sync
    TestClient) so both requests genuinely share one event loop, the way
    uvicorn's real requests do — a sync TestClient's portal doesn't
    reproduce the freeze.
    """
    raw_path = root_dir / 'photo.dng'
    raw_path.write_bytes(b'raw-bytes')
    _insert_file_row(str(root_dir), 'photo.dng', 'rawhash', media_type='photo')

    other_path = root_dir / 'other.jpg'
    other_path.write_bytes(b'x')
    _insert_file_row(str(root_dir), 'other.jpg', 'otherhash', media_type='photo')

    # Released by a plain background thread (independent of the event loop),
    # after a short real delay — simulating the render taking a while.
    render_delay_s = 0.3
    release_render = threading.Event()
    threading.Timer(render_delay_s, release_render.set).start()

    def slow_render(file_path):
        assert release_render.wait(timeout=5), 'test never released the slow render'
        return b'jpeg-bytes'

    monkeypatch.setattr(files_api, 'render_full_raw', slow_render)

    app = FastAPI()
    app.include_router(FilesApi)

    async def run(start):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            async def timed(coro):
                resp = await coro
                return resp, time.monotonic() - start

            return await asyncio.gather(
                timed(client.get('/files/full-image/rawhash')),
                timed(client.get('/files/details', params={'path': str(other_path)})),
            )

    overall_start = time.monotonic()
    (full_resp, full_elapsed), (other_resp, other_elapsed) = asyncio.run(run(overall_start))

    assert full_resp.status_code == 200
    assert full_resp.content == b'jpeg-bytes'
    assert other_resp.status_code == 200
    # Measured from before either request was issued: the "other" request
    # must come back well before the render releases — i.e. it must have
    # actually run concurrently with the render, not merely been fast once
    # its turn finally came after the render blocked everything else.
    assert other_elapsed < render_delay_s / 2, (
        f'other request took {other_elapsed:.2f}s since both were issued '
        f'(render delay {render_delay_s}s) — looks like it was blocked behind the render'
    )


def test_config_reports_trash_dir_name(root_dir, monkeypatch):
    monkeypatch.setenv('TRASH_DIR_NAME', '.my-trash')
    app = FastAPI()
    app.include_router(ConfigApi)
    client = TestClient(app)

    resp = client.get('/config')
    assert resp.status_code == 200
    assert resp.json()['trash_dir_name'] == '.my-trash'
