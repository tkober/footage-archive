"""HTTP-level smoke test for GET /config, including the #107 POI Map ID
field (google_maps_map_id_poi): present and empty by default, and reflects
the GOOGLE_MAPS_MAP_ID_POI env var when set."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.config import ConfigApi


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(ConfigApi)
    return TestClient(app)


def test_config_shape_default_empty(monkeypatch):
    monkeypatch.delenv('GOOGLE_MAPS_MAP_ID_POI', raising=False)
    client = _make_client()
    resp = client.get('/config')
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        'root_dir', 'task_poll_interval_ms', 'browser_hidden_extensions',
        'google_maps_api_key', 'google_maps_map_id', 'google_maps_map_id_poi',
        'trash_dir_name',
    ):
        assert key in body
    assert body['google_maps_map_id_poi'] == ''


def test_config_reflects_poi_map_id_env_var(monkeypatch):
    monkeypatch.setenv('GOOGLE_MAPS_MAP_ID_POI', 'poi-map-id-123')
    client = _make_client()
    resp = client.get('/config')
    assert resp.status_code == 200
    assert resp.json()['google_maps_map_id_poi'] == 'poi-map-id-123'
