from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StrictStr

from tasks.activity import Activity
from tasks.taskmanager import Task, TaskManager, TaskStatus

TasksApi = APIRouter(prefix='/tasks')


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


@TasksApi.get('/')
async def get_tasks() -> List[TaskDescription]:
    return [describe(t) for t in TaskManager().get_all_tasks()]


@TasksApi.get('/{task_id}')
async def get_task(task_id: str) -> TaskDescription:
    task = TaskManager().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail='Task not found')

    return describe(task)


@TasksApi.delete('/completed')
async def clear_completed_tasks() -> List[TaskDescription]:
    return [
        TaskDescription(**t.model_dump())
        for t in TaskManager().clear_completed_tasks()
    ]


@TasksApi.delete('/{task_id}')
async def delete_task(task_id: str) -> TaskDescription:
    task = TaskManager().delete_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail='Task not found')
    return TaskDescription(**task.model_dump())
