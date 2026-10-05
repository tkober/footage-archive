"""Unit tests for tasks/loadcontrol.py (#71): sysfs temperature reading,
cgroup limit parsing, the heavy-job semaphore's concurrency cap, and the
throttle guard. No DB/Postgres needed."""

import threading
import time

import pytest

from tasks import loadcontrol


# --- read_cpu_temperature ---------------------------------------------------

def test_cpu_temperature_from_coretemp_hwmon(tmp_path):
    hwmon_root = tmp_path / 'hwmon'
    hwmon0 = hwmon_root / 'hwmon0'
    hwmon0.mkdir(parents=True)
    (hwmon0 / 'name').write_text('coretemp\n')
    (hwmon0 / 'temp1_input').write_text('45000\n')
    (hwmon0 / 'temp2_input').write_text('67500\n')

    temp = loadcontrol.read_cpu_temperature(hwmon_root=hwmon_root, thermal_zone_root=tmp_path / 'nope')
    assert temp == 67.5


def test_cpu_temperature_ignores_non_cpu_hwmon(tmp_path):
    hwmon_root = tmp_path / 'hwmon'
    hwmon0 = hwmon_root / 'hwmon0'
    hwmon0.mkdir(parents=True)
    (hwmon0 / 'name').write_text('nvme\n')
    (hwmon0 / 'temp1_input').write_text('99000\n')

    temp = loadcontrol.read_cpu_temperature(hwmon_root=hwmon_root, thermal_zone_root=tmp_path / 'nope')
    assert temp is None


def test_cpu_temperature_falls_back_to_thermal_zone(tmp_path):
    hwmon_root = tmp_path / 'hwmon'  # does not exist -> hwmon lookup finds nothing
    thermal_root = tmp_path / 'thermal'
    zone0 = thermal_root / 'thermal_zone0'
    zone0.mkdir(parents=True)
    (zone0 / 'type').write_text('x86_pkg_temp\n')
    (zone0 / 'temp').write_text('72300\n')

    temp = loadcontrol.read_cpu_temperature(hwmon_root=hwmon_root, thermal_zone_root=thermal_root)
    assert temp == 72.3


def test_cpu_temperature_none_when_nothing_readable(tmp_path):
    temp = loadcontrol.read_cpu_temperature(hwmon_root=tmp_path / 'a', thermal_zone_root=tmp_path / 'b')
    assert temp is None


# --- read_cpu_limit ----------------------------------------------------------

def test_cpu_limit_unlimited(tmp_path):
    cpu_max = tmp_path / 'cpu.max'
    cpu_max.write_text('max 100000\n')
    assert loadcontrol.read_cpu_limit(cgroup_cpu_max=cpu_max) is None


def test_cpu_limit_parsed(tmp_path):
    cpu_max = tmp_path / 'cpu.max'
    cpu_max.write_text('400000 100000\n')
    assert loadcontrol.read_cpu_limit(cgroup_cpu_max=cpu_max) == 4.0


def test_cpu_limit_missing_file(tmp_path):
    assert loadcontrol.read_cpu_limit(cgroup_cpu_max=tmp_path / 'does-not-exist') is None


# --- heavy_slot concurrency cap ----------------------------------------------

@pytest.fixture(autouse=True)
def _reset_loadcontrol_state(monkeypatch):
    """Each test gets a fresh semaphore/stats, and the throttle guard sees no
    sensor/limit by default so heavy_slot doesn't block on real host state."""
    monkeypatch.setattr(loadcontrol, '_semaphore', None)
    monkeypatch.setattr(loadcontrol, '_stats', {
        'active': 0, 'waiting': 0, 'heavy_jobs_total': 0,
        'heavy_jobs_seconds_total': 0.0, 'throttle_events': 0,
        'throttled': False, 'throttle_reason': None, 'last_slow_job': None,
    })
    monkeypatch.setattr(loadcontrol, '_throttle_reason', lambda: None)
    yield


def test_heavy_slot_caps_concurrency(monkeypatch):
    monkeypatch.setenv('HEAVY_JOB_CONCURRENCY', '2')

    max_concurrent = 0
    current = 0
    lock = threading.Lock()

    def worker():
        nonlocal max_concurrent, current
        with loadcontrol.heavy_slot('test job'):
            with lock:
                current += 1
                max_concurrent = max(max_concurrent, current)
            time.sleep(0.1)
            with lock:
                current -= 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert max_concurrent == 2


def test_heavy_slot_tracks_stats(monkeypatch):
    monkeypatch.setenv('HEAVY_JOB_CONCURRENCY', '3')
    with loadcontrol.heavy_slot('job a'):
        pass
    with loadcontrol.heavy_slot('job b'):
        pass
    snapshot = loadcontrol.diagnostics()
    assert snapshot['heavy_jobs_total'] == 2
    assert snapshot['active_heavy_jobs'] == 0


# --- throttle guard -----------------------------------------------------------

def test_throttle_guard_waits_then_proceeds(monkeypatch):
    monkeypatch.setenv('HEAVY_JOB_CONCURRENCY', '2')
    monkeypatch.setattr(loadcontrol, 'THROTTLE_SLEEP_S', 0)

    reasons = iter(['CPU temperature 90.0°C >= limit 85.0°C', 'CPU temperature 90.0°C >= limit 85.0°C', None])
    monkeypatch.setattr(loadcontrol, '_throttle_reason', lambda: next(reasons, None))

    sleep_calls = []
    monkeypatch.setattr(loadcontrol.time, 'sleep', lambda s: sleep_calls.append(s))

    entered = []
    with loadcontrol.heavy_slot('throttled job'):
        entered.append(True)

    assert entered == [True]
    assert len(sleep_calls) == 2  # slept while throttled, then proceeded
    snapshot = loadcontrol.diagnostics()
    assert snapshot['throttled'] is False
    assert snapshot['throttle_events'] == 1
