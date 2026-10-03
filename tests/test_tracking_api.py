"""HTTP-level smoke test for POST /tracking/rediscover's validation (400/403).
Running the task body itself is covered directly (not via TestClient/
BackgroundTasks) in tests/test_rediscover.py."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.tracking import TrackingApi


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(TrackingApi)
    return TestClient(app)


def test_rediscover_outside_root_dir_is_403(db, root_dir):
    client = _make_client()
    resp = client.post('/tracking/rediscover', json={'path': '/etc'})
    assert resp.status_code == 403


def test_rediscover_non_directory_is_400(db, root_dir):
    client = _make_client()
    file_path = root_dir / 'f.jpg'
    file_path.write_bytes(b'x')
    resp = client.post('/tracking/rediscover', json={'path': str(file_path)})
    assert resp.status_code == 400
