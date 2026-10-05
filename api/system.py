from fastapi import APIRouter

from api.dtos import LastSlowJob, LoadAvg, SystemDiagnostics, SystemDiagnosticsResponse, SystemSettings
from env.environment import Environment
from tasks.loadcontrol import diagnostics

SystemApi = APIRouter(prefix='/system')

_env = Environment()


@SystemApi.get('/diagnostics')
async def get_diagnostics() -> SystemDiagnosticsResponse:
    """Read-only snapshot of the effective load-management settings plus
    live runtime diagnostics (CPU temperature, load average, heavy-job
    throttling) — see #71. Settings become editable here in a follow-up."""
    snapshot = diagnostics()
    load_avg = snapshot['load_avg']
    last_slow_job = snapshot.get('last_slow_job')

    return SystemDiagnosticsResponse(
        settings=SystemSettings(
            worker_pool_size=_env.get_worker_pool_size(),
            db_pool_size=_env.get_db_pool_size(),
            db_max_overflow=_env.get_db_max_overflow(),
            heavy_job_concurrency=_env.get_heavy_job_concurrency(),
            ffmpeg_threads=_env.get_ffmpeg_threads(),
            process_niceness=_env.get_process_niceness(),
            cpu_temp_limit_c=_env.get_cpu_temp_limit_c(),
            load_avg_limit=_env.get_load_avg_limit(),
        ),
        runtime=SystemDiagnostics(
            cpu_count=snapshot['cpu_count'],
            cpu_limit=snapshot['cpu_limit'],
            load_avg=LoadAvg(load_1m=load_avg['1m'], load_5m=load_avg['5m'], load_15m=load_avg['15m']),
            cpu_temperature_c=snapshot['cpu_temperature_c'],
            throttled=snapshot['throttled'],
            throttle_reason=snapshot['throttle_reason'],
            active_heavy_jobs=snapshot['active_heavy_jobs'],
            waiting_heavy_jobs=snapshot['waiting_heavy_jobs'],
            heavy_jobs_total=snapshot['heavy_jobs_total'],
            heavy_jobs_seconds_total=snapshot['heavy_jobs_seconds_total'],
            throttle_events=snapshot['throttle_events'],
            last_slow_job=LastSlowJob(**last_slow_job) if last_slow_job else None,
            pool_queue_length=snapshot['pool_queue_length'],
        ),
    )
