from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StrictStr

from db.database import Database, InvalidScanTransitionError, ScanJobNotFoundError
from tasks import scanqueue
from tasks.activity import Activity
from tasks.taskmanager import Task, TaskManager, TaskStatus

TasksApi = APIRouter(prefix='/tasks')

# ScanJobs.status -> TaskStatus (#137): a synthetic task entry is added for
# every job that isn't PLANNED (planning-only, #138, never shown here).
# CANCELLED maps to COMPLETED, same as the widget's existing "FAILED stays
# visible, everything else that's done is COMPLETED" rule — its progress
# text (the job's summary) says "cancelled", same as #134/#135/#136's tasks
# already do for a FAILED/partial outcome in plain text.
_JOB_STATUS_TO_TASK_STATUS = {
    'QUEUED': TaskStatus.QUEUED,
    'PAUSED': TaskStatus.QUEUED,
    'RUNNING': TaskStatus.RUNNING,
    'DONE': TaskStatus.COMPLETED,
    'FAILED': TaskStatus.FAILED,
    'CANCELLED': TaskStatus.COMPLETED,
}
_TERMINAL_JOB_STATUSES = ('DONE', 'FAILED', 'CANCELLED')


class TaskDescription(BaseModel):
    id: StrictStr
    name: StrictStr
    description: StrictStr
    status: TaskStatus
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    last_updated: datetime
    error: Optional[str] = None
    progress: Optional[str] = None
    # Only set while RUNNING (#93): ACTIVE = doing work right now, otherwise
    # what it is waiting for (worker pool / heavy-job slot / throttle).
    activity: Optional[Activity] = None


def describe(task: Task) -> TaskDescription:
    return TaskDescription(**task.model_dump(), activity=TaskManager().get_activity(task.id))


def _job_progress_counts(job: dict) -> tuple[int, int, int, int]:
    """(units_done, units_total, files_done, files_total) from whichever
    shape `job` has: `Database.get_scan_jobs`'s aggregated dict (used for
    the bulk GET /tasks listing) already carries these; `Database.get_scan_job`'s
    detail dict (GET/DELETE /tasks/{id}) only has the `units` list, so they're
    computed here from that instead — see Database.get_scan_jobs's docstring
    for the exact "done"/"total" definitions this mirrors."""
    if 'units_done' in job:
        return job['units_done'], job['units_total'], job['files_done'], job['files_total']
    units = job.get('units', [])
    done = [u for u in units if u['status'] in _TERMINAL_JOB_STATUSES]
    counted = [u for u in units if u['status'] != 'DESELECTED']
    files_done = sum(u.get('media_file_count') or 0 for u in done)
    files_total = sum(u.get('media_file_count') or 0 for u in counted)
    return len(done), len(counted), files_done, files_total


def _job_progress_text(job: dict) -> Optional[str]:
    if job['status'] in _TERMINAL_JOB_STATUSES:
        return job.get('summary')
    units_done, units_total, files_done, files_total = _job_progress_counts(job)
    return f'{units_done} / {units_total} folders · {files_done} / {files_total} files'


def _job_activity(job_id: str, job_status: str) -> Optional[Activity]:
    """ACTIVE if any of the job's RUNNING units is actually working right
    now, else the first (by position) non-None waiting activity found among
    them, else None — same "only set while RUNNING" convention as
    TaskDescription.activity. Needs the full unit list, which neither shape
    of `job` is guaranteed to carry, so it fetches its own."""
    if job_status != 'RUNNING':
        return None
    full = Database().get_scan_job(job_id)
    if full is None:
        return None
    activities = [
        a for a in (scanqueue.get_activity(u['id']) for u in full['units'] if u['status'] == 'RUNNING')
        if a is not None
    ]
    if Activity.ACTIVE in activities:
        return Activity.ACTIVE
    return activities[0] if activities else None


def _describe_job(job: dict) -> TaskDescription:
    return TaskDescription(
        id=job['id'],
        name='Scan directory',
        description=f'Scanning directory "{job["root_path"]}".',
        status=_JOB_STATUS_TO_TASK_STATUS[job['status']],
        started_at=job.get('started_at'),
        last_updated=job.get('finished_at') or job.get('started_at') or job['created_at'],
        progress=_job_progress_text(job),
        activity=_job_activity(job['id'], job['status']),
    )


def _scan_job_tasks() -> List[TaskDescription]:
    jobs = Database().get_scan_jobs(active=None)
    return [_describe_job(job) for job in jobs if job['status'] != 'PLANNED']


@TasksApi.get('/')
async def get_tasks() -> List[TaskDescription]:
    return [describe(t) for t in TaskManager().get_all_tasks()] + _scan_job_tasks()


@TasksApi.get('/{task_id}')
async def get_task(task_id: str) -> TaskDescription:
    task = TaskManager().get_task(task_id)
    if task is not None:
        return describe(task)

    job = Database().get_scan_job(task_id)
    if job is not None and job['status'] != 'PLANNED':
        return _describe_job(job)

    raise HTTPException(status_code=404, detail='Task not found')


@TasksApi.delete('/completed')
async def clear_completed_tasks() -> List[TaskDescription]:
    """Clears COMPLETED TaskManager tasks as before, plus every finished
    (DONE/CANCELLED — not FAILED, same asymmetry as the plain tasks: those
    stay for individual removal) scan job, deleting each from the queue."""
    cleared = [TaskDescription(**t.model_dump()) for t in TaskManager().clear_completed_tasks()]

    for job in Database().get_scan_jobs(active=None):
        if job['status'] not in ('DONE', 'CANCELLED'):
            continue
        described = _describe_job(job)
        try:
            Database().delete_scan_job(job['id'])
        except (ScanJobNotFoundError, InvalidScanTransitionError):
            continue
        cleared.append(described)

    return cleared


@TasksApi.delete('/{task_id}')
async def delete_task(task_id: str) -> TaskDescription:
    task = TaskManager().delete_task(task_id)
    if task is not None:
        return TaskDescription(**task.model_dump())

    job = Database().get_scan_job(task_id)
    if job is None or job['status'] == 'PLANNED':
        raise HTTPException(status_code=404, detail='Task not found')
    described = _describe_job(job)
    try:
        Database().delete_scan_job(task_id)
    except ScanJobNotFoundError:
        raise HTTPException(status_code=404, detail='Task not found')
    except InvalidScanTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail)
    return described
