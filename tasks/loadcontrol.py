"""Process-wide load management for CPU-heavy jobs (ffmpeg, exiftool, raw
decoding, etc.) — see issue #71.

The shared worker pool (`tasks/workerpool.py`) bounds how many *tasks* run
concurrently, but each task can itself spawn several CPU-hungry subprocesses
(ffmpeg spawns one decoding thread per core by default), and tasks started
via FastAPI BackgroundTasks run outside that pool entirely. Without a second,
global ceiling on truly heavy work, a handful of concurrent video scans can
pin every core at 100% for long stretches and drive the host CPU temperature
into thermal shutdown territory.

This module adds that ceiling (`heavy_slot`) plus a temperature/load-aware
throttle that pauses new heavy work when the host is already hot or loaded,
and a couple of small helpers (`run_niced`, sysfs readers) used by the
ffmpeg/exiftool call sites.
"""

import logging
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from env.environment import Environment
from tasks import activity

logger = logging.getLogger(__name__)

# How long to sleep between throttle-guard checks. A module-level constant
# (not a function default) so tests can monkeypatch it to run fast.
THROTTLE_SLEEP_S = 5

# Default root for sysfs sensor lookups — overridable (module constant) so
# tests can point it at a fake tree.
HWMON_ROOT = Path('/sys/class/hwmon')
THERMAL_ZONE_ROOT = Path('/sys/class/thermal')
CGROUP_CPU_MAX = Path('/sys/fs/cgroup/cpu.max')
# Not namespaced: inside a container this is the whole host, which is what
# matters for heat.
PROC_STAT = Path('/proc/stat')

# Previous /proc/stat reading (monotonic time, busy jiffies, total jiffies) —
# CPU usage is the delta to it, so pollers get "usage since my last poll".
_CPU_SAMPLE_MAX_AGE_S = 60
_CPU_SAMPLE_WINDOW_S = 0.25
_cpu_sample: tuple[float, int, int] | None = None
_cpu_sample_lock = threading.Lock()

_HWMON_NAMES = {'coretemp', 'k10temp', 'zenpower', 'cpu_thermal'}

_semaphore: threading.BoundedSemaphore | None = None
_semaphore_lock = threading.Lock()

# Separate, size-1 semaphore for interactive (user-triggered) renders — see
# heavy_slot's `interactive` parameter.
_interactive_semaphore: threading.BoundedSemaphore | None = None
_interactive_semaphore_lock = threading.Lock()

_stats_lock = threading.Lock()
_stats = {
    'active': 0,
    'waiting': 0,
    'heavy_jobs_total': 0,
    'heavy_jobs_seconds_total': 0.0,
    'throttle_events': 0,
    'throttled': False,
    'throttle_reason': None,
    'last_slow_job': None,  # {'label': str, 'duration_s': float}
}


def _get_semaphore() -> threading.BoundedSemaphore:
    global _semaphore
    if _semaphore is None:
        with _semaphore_lock:
            if _semaphore is None:
                size = Environment().get_heavy_job_concurrency()
                _semaphore = threading.BoundedSemaphore(size)
    return _semaphore


def _get_interactive_semaphore() -> threading.BoundedSemaphore:
    global _interactive_semaphore
    if _interactive_semaphore is None:
        with _interactive_semaphore_lock:
            if _interactive_semaphore is None:
                _interactive_semaphore = threading.BoundedSemaphore(1)
    return _interactive_semaphore


def read_cpu_temperature(hwmon_root: Path = HWMON_ROOT, thermal_zone_root: Path = THERMAL_ZONE_ROOT) -> float | None:
    """Best-effort CPU package/core temperature in °C, or None if no readable
    sensor is found. Tries hwmon (coretemp/k10temp/zenpower/cpu_thermal) first,
    then falls back to the generic thermal_zone x86_pkg_temp/cpu zone."""
    temps = []
    try:
        if hwmon_root.is_dir():
            for hwmon_dir in sorted(hwmon_root.glob('hwmon*')):
                name_file = hwmon_dir / 'name'
                if not name_file.is_file():
                    continue
                try:
                    name = name_file.read_text().strip()
                except OSError:
                    continue
                if name not in _HWMON_NAMES:
                    continue
                for temp_file in hwmon_dir.glob('temp*_input'):
                    try:
                        millidegrees = int(temp_file.read_text().strip())
                        temps.append(millidegrees / 1000.0)
                    except (OSError, ValueError):
                        continue
    except OSError:
        pass

    if temps:
        return max(temps)

    try:
        if thermal_zone_root.is_dir():
            for zone_dir in sorted(thermal_zone_root.glob('thermal_zone*')):
                type_file = zone_dir / 'type'
                temp_file = zone_dir / 'temp'
                if not type_file.is_file() or not temp_file.is_file():
                    continue
                try:
                    zone_type = type_file.read_text().strip()
                except OSError:
                    continue
                if zone_type != 'x86_pkg_temp' and 'cpu' not in zone_type.lower():
                    continue
                try:
                    millidegrees = int(temp_file.read_text().strip())
                    temps.append(millidegrees / 1000.0)
                except (OSError, ValueError):
                    continue
    except OSError:
        pass

    return max(temps) if temps else None


def read_cpu_limit(cgroup_cpu_max: Path = CGROUP_CPU_MAX) -> float | None:
    """Effective CPU core limit from cgroup v2 cpu.max ("<quota> <period>"),
    e.g. "400000 100000" -> 4.0 cores. "max ..." (unlimited) -> None."""
    try:
        content = cgroup_cpu_max.read_text().strip()
    except OSError:
        return None
    parts = content.split()
    if len(parts) != 2:
        return None
    quota_str, period_str = parts
    if quota_str == 'max':
        return None
    try:
        quota = float(quota_str)
        period = float(period_str)
        if period <= 0:
            return None
        return quota / period
    except ValueError:
        return None


def read_cpu_times(proc_stat: Path = PROC_STAT) -> tuple[int, int] | None:
    """(busy, total) jiffies from the aggregate `cpu` line of /proc/stat, or
    None if unreadable. Idle = idle + iowait; guest time is already included
    in user/nice, so only the first eight fields are summed."""
    try:
        with proc_stat.open() as f:
            fields = f.readline().split()
        if not fields or fields[0] != 'cpu':
            return None
        values = [int(v) for v in fields[1:9]]
    except (OSError, ValueError):
        return None
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total - idle, total


def cpu_usage_between(previous: tuple[int, int], current: tuple[int, int]) -> float | None:
    """CPU usage in percent between two `read_cpu_times()` readings."""
    busy = current[0] - previous[0]
    total = current[1] - previous[1]
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * busy / total)), 1)


def read_cpu_usage() -> float | None:
    """Host-wide CPU usage in percent since the previous call (the frontend
    polls every few seconds). Without a recent previous reading it samples
    over a short window instead, which blocks briefly — call it off the
    event loop."""
    global _cpu_sample
    with _cpu_sample_lock:
        now = time.monotonic()
        previous = _cpu_sample
        if previous is None or now - previous[0] > _CPU_SAMPLE_MAX_AGE_S:
            first = read_cpu_times()
            if first is None:
                return None
            time.sleep(_CPU_SAMPLE_WINDOW_S)
            previous = (time.monotonic(), *first)
        current = read_cpu_times()
        if current is None:
            return None
        _cpu_sample = (time.monotonic(), *current)
        return cpu_usage_between(previous[1:], current)


def _throttle_reason() -> str | None:
    """Returns a human-readable reason if the host is currently over its
    configured temperature/load limits, else None. Either check is skipped
    when its limit is 0 (disabled) or its reading is unavailable."""
    env = Environment()

    temp_limit = env.get_cpu_temp_limit_c()
    if temp_limit > 0:
        temp = read_cpu_temperature()
        if temp is not None and temp >= temp_limit:
            return f'CPU temperature {temp:.1f}°C >= limit {temp_limit:.1f}°C'

    load_limit = env.get_load_avg_limit()
    if load_limit > 0:
        load1 = os.getloadavg()[0]
        if load1 >= load_limit:
            return f'1-min load average {load1:.2f} >= limit {load_limit:.2f}'

    return None


def _wait_while_throttled():
    # Start/end are logged on the global state transition, so several waiting
    # jobs produce one warning per throttle episode, not one each.
    was_throttled = False
    while True:
        reason = _throttle_reason()
        if reason is None:
            break
        with _stats_lock:
            started = not _stats['throttled']
            _stats['throttled'] = True
            _stats['throttle_reason'] = reason
            if started:
                _stats['throttle_events'] += 1
        if started:
            logger.warning(f'Throttling heavy jobs: {reason}')
        was_throttled = True
        time.sleep(THROTTLE_SLEEP_S)

    if was_throttled:
        with _stats_lock:
            ended = _stats['throttled']
            _stats['throttled'] = False
            _stats['throttle_reason'] = None
        if ended:
            logger.info('Throttling ended, resuming heavy jobs')


@contextmanager
def heavy_slot(label: str, interactive: bool = False):
    """Context manager guarding a CPU-heavy unit of work (ffmpeg/exiftool/raw
    decode). Waits for the host's temperature/load to be back under the
    configured limits, then acquires a slot in the global heavy-job
    semaphore (size = HEAVY_JOB_CONCURRENCY) for the duration of the block.

    Use this around a whole logical job (e.g. "generate this clip preview",
    not each individual ffmpeg call within it) so the semaphore reflects real
    concurrent heavy work.

    `interactive=True` is for a single user-triggered render (e.g. "Load full
    resolution"): it skips the throttle wait and the batch semaphore
    entirely, using its own size-1 semaphore instead. A single render is
    short — the throttle exists to protect the host from sustained batch
    work, not a one-off — but the size-1 cap still keeps several interactive
    renders (e.g. from the comparison view) from piling up concurrently.
    """
    # "waiting" covers both a throttle pause and a full semaphore.
    with _stats_lock:
        _stats['waiting'] += 1
    try:
        with activity.waiting_for_heavy_slot():
            if interactive:
                semaphore = _get_interactive_semaphore()
            else:
                _wait_while_throttled()
                semaphore = _get_semaphore()
            semaphore.acquire()
    finally:
        with _stats_lock:
            _stats['waiting'] -= 1
    with _stats_lock:
        _stats['active'] += 1

    start = time.monotonic()
    try:
        yield
    finally:
        duration = time.monotonic() - start
        semaphore.release()
        with _stats_lock:
            _stats['active'] -= 1
            _stats['heavy_jobs_total'] += 1
            _stats['heavy_jobs_seconds_total'] += duration
            if duration > 60:
                _stats['last_slow_job'] = {'label': label, 'duration_s': duration}
        if duration > 60:
            logger.warning(f'Heavy job "{label}" took {duration:.1f}s')


def nice_preexec():
    """preexec_fn for subprocess.run that lowers the child's scheduling
    priority (os.nice). Best-effort — a permission error is ignored."""
    try:
        os.nice(Environment().get_process_niceness())
    except OSError:
        pass


def run_niced(cmd, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run wrapper that runs `cmd` at lower scheduling priority
    (see nice_preexec), so ffmpeg/ffprobe/exiftool don't compete evenly with
    the rest of the system for CPU time."""
    return subprocess.run(cmd, preexec_fn=nice_preexec, **kwargs)


def is_throttled() -> bool:
    """Whether heavy jobs are currently paused by the throttle guard."""
    with _stats_lock:
        return bool(_stats['throttled'])


def diagnostics() -> dict:
    """Snapshot of current load-management state, for logging/diagnostics
    and the /system/diagnostics API endpoint."""
    from tasks.workerpool import get_worker_pool

    with _stats_lock:
        stats = dict(_stats)

    pool_queue_length = None
    try:
        pool = get_worker_pool()
        pool_queue_length = pool._work_queue.qsize()
    except Exception:
        pool_queue_length = None

    load1, load5, load15 = os.getloadavg()

    return {
        'cpu_count': os.cpu_count(),
        'cpu_limit': read_cpu_limit(),
        'load_avg': {'1m': load1, '5m': load5, '15m': load15},
        'cpu_usage_percent': read_cpu_usage(),
        'cpu_temperature_c': read_cpu_temperature(),
        'throttled': stats['throttled'],
        'throttle_reason': stats['throttle_reason'],
        'active_heavy_jobs': stats['active'],
        'waiting_heavy_jobs': stats['waiting'],
        'heavy_jobs_total': stats['heavy_jobs_total'],
        'heavy_jobs_seconds_total': stats['heavy_jobs_seconds_total'],
        'throttle_events': stats['throttle_events'],
        'last_slow_job': stats['last_slow_job'],
        'pool_queue_length': pool_queue_length,
    }
