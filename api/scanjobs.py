"""Persistent scan queue API (#137, epic #133) — jobs made of units (one
directory each, no recursion), worked off by tasks/scanqueue.py's consumer
threads. Planning a whole tree into many units (#138) isn't here yet;
`POST /tracking/scan-directory` still creates its own recursive TaskManager
task, unaffected by this queue (see api/tracking.py)."""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy.exc import IntegrityError

from api.dtos import ScanJobDto, ScanJobListEntry
from db.database import (
    Database,
    DirectoryAlreadyQueuedError,
    InvalidScanTransitionError,
    ScanJobNotFoundError,
    ScanUnitNotFoundError,
)
from tasks import scanqueue

ScanJobsApi = APIRouter(prefix='/scan-jobs')


def _job_dto(job: dict) -> ScanJobDto:
    units = [
        {**unit, 'activity': scanqueue.get_activity(unit['id']) if unit['status'] == 'RUNNING' else None}
        for unit in job.get('units', [])
    ]
    return ScanJobDto(**{**job, 'units': units})


@ScanJobsApi.get('')
async def get_scan_jobs(active: Optional[bool] = None) -> List[ScanJobListEntry]:
    return [ScanJobListEntry(**job) for job in Database().get_scan_jobs(active=active)]


@ScanJobsApi.get('/{job_id}')
async def get_scan_job(job_id: str) -> ScanJobDto:
    job = Database().get_scan_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Scan job not found')
    return _job_dto(job)


@ScanJobsApi.post('/{job_id}/start')
async def start_scan_job(job_id: str) -> ScanJobDto:
    try:
        job = Database().start_scan_job(job_id)
    except ScanJobNotFoundError:
        raise HTTPException(status_code=404, detail='Scan job not found')
    except InvalidScanTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail)
    except IntegrityError:
        # Defensive: start_scan_job already catches this per unit; only a
        # genuinely unexpected race over the partial unique index gets here.
        raise HTTPException(status_code=409, detail='A unit\'s directory is already queued or running elsewhere')
    return _job_dto(job)


@ScanJobsApi.post('/{job_id}/pause')
async def pause_scan_job(job_id: str) -> ScanJobDto:
    try:
        job = Database().pause_scan_job(job_id)
    except ScanJobNotFoundError:
        raise HTTPException(status_code=404, detail='Scan job not found')
    except InvalidScanTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail)
    return _job_dto(job)


@ScanJobsApi.post('/{job_id}/resume')
async def resume_scan_job(job_id: str) -> ScanJobDto:
    try:
        job = Database().resume_scan_job(job_id, scanqueue.summarize_job)
    except ScanJobNotFoundError:
        raise HTTPException(status_code=404, detail='Scan job not found')
    except InvalidScanTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail)
    return _job_dto(job)


@ScanJobsApi.post('/{job_id}/cancel')
async def cancel_scan_job(job_id: str) -> ScanJobDto:
    try:
        job = Database().cancel_scan_job(job_id, scanqueue.summarize_job)
    except ScanJobNotFoundError:
        raise HTTPException(status_code=404, detail='Scan job not found')
    except InvalidScanTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail)
    # Any unit this process has RUNNING for the job notices on its next
    # per-file should_cancel() check — see tasks/scanqueue.py.
    scanqueue.flag_job_cancelled(job_id)
    return _job_dto(job)


@ScanJobsApi.delete('/{job_id}')
async def delete_scan_job(job_id: str) -> Response:
    try:
        Database().delete_scan_job(job_id)
    except ScanJobNotFoundError:
        raise HTTPException(status_code=404, detail='Scan job not found')
    except InvalidScanTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail)
    return Response(status_code=204)


@ScanJobsApi.post('/{job_id}/units/{unit_id}/cancel')
async def cancel_scan_unit(job_id: str, unit_id: int) -> ScanJobDto:
    try:
        cancelled_in_db = Database().cancel_scan_unit(job_id, unit_id, scanqueue.summarize_job)
    except ScanUnitNotFoundError:
        raise HTTPException(status_code=404, detail='Scan unit not found')
    except InvalidScanTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail)
    if not cancelled_in_db:
        scanqueue.flag_unit_cancelled(unit_id)
    return _job_dto(Database().get_scan_job(job_id))


@ScanJobsApi.post('/{job_id}/units/{unit_id}/retry')
async def retry_scan_unit(job_id: str, unit_id: int) -> ScanJobDto:
    try:
        job = Database().retry_scan_unit(job_id, unit_id, scanqueue.summarize_job)
    except ScanUnitNotFoundError:
        raise HTTPException(status_code=404, detail='Scan unit not found')
    except InvalidScanTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail)
    except DirectoryAlreadyQueuedError:
        raise HTTPException(status_code=409, detail='Directory is already queued or running in another job')
    return _job_dto(job)


@ScanJobsApi.post('/{job_id}/units/{unit_id}/move-top')
async def move_scan_unit_to_top(job_id: str, unit_id: int) -> ScanJobDto:
    try:
        job = Database().move_scan_unit_to_top(job_id, unit_id)
    except ScanUnitNotFoundError:
        raise HTTPException(status_code=404, detail='Scan unit not found')
    except InvalidScanTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail)
    return _job_dto(job)


@ScanJobsApi.post('/{job_id}/units/{unit_id}/deselect')
async def deselect_scan_unit(job_id: str, unit_id: int) -> ScanJobDto:
    try:
        job = Database().deselect_scan_unit(job_id, unit_id)
    except ScanUnitNotFoundError:
        raise HTTPException(status_code=404, detail='Scan unit not found')
    except InvalidScanTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail)
    return _job_dto(job)


@ScanJobsApi.post('/{job_id}/units/{unit_id}/reselect')
async def reselect_scan_unit(job_id: str, unit_id: int) -> ScanJobDto:
    try:
        job = Database().reselect_scan_unit(job_id, unit_id)
    except ScanUnitNotFoundError:
        raise HTTPException(status_code=404, detail='Scan unit not found')
    except InvalidScanTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail)
    return _job_dto(job)
