"""HTTP-level smoke test for GET /system/diagnostics (#71, read-only
worker/pool + runtime diagnostics for the Settings page)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.system import SystemApi


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(SystemApi)
    return TestClient(app)


def test_diagnostics_shape():
    client = _make_client()
    resp = client.get('/system/diagnostics')
    assert resp.status_code == 200
    body = resp.json()

    settings = body['settings']
    for key in (
        'worker_pool_size', 'db_pool_size', 'db_max_overflow', 'heavy_job_concurrency',
        'ffmpeg_threads', 'process_niceness', 'cpu_temp_limit_c', 'load_avg_limit',
    ):
        assert key in settings

    runtime = body['runtime']
    for key in (
        'cpu_count', 'cpu_limit', 'load_avg', 'cpu_temperature_c', 'throttled',
        'throttle_reason', 'active_heavy_jobs', 'waiting_heavy_jobs',
        'heavy_jobs_total', 'heavy_jobs_seconds_total', 'throttle_events', 'last_slow_job',
    ):
        assert key in runtime
    assert set(runtime['load_avg'].keys()) == {'load_1m', 'load_5m', 'load_15m'}
