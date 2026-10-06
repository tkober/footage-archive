import uuid
from datetime import datetime
from enum import Enum
from typing import Callable, Dict, Optional, List

from fastapi import BackgroundTasks
from pydantic import BaseModel

from tasks import activity
from tasks.activity import Activity, TaskActivity
from tasks.loadcontrol import is_throttled


class TaskStatus(str, Enum):
    PENDING = "PENDING",
    QUEUED = "QUEUED",
    RUNNING = "RUNNING",
    COMPLETED = "COMPLETED",
    FAILED = "FAILED",


class TaskRequest(BaseModel):
    name: str
    description: str
    method: Callable


class Task(TaskRequest):
    id: str
    status: TaskStatus = TaskStatus.PENDING
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    last_updated: datetime
    error: Optional[str] = None
    progress: Optional[str] = None


class TaskManager:
    _instance = None
    _tasks: Dict[str, Task] = {}
    # Live activity counters of RUNNING tasks (#93), dropped once they finish.
    _activities: Dict[str, TaskActivity] = {}

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(TaskManager, cls).__new__(cls)
        return cls._instance

    def __init__(self, value=None):
        if not hasattr(self, 'value'):
            self.value = value

    def request_task(self, request: TaskRequest, background_tasks: BackgroundTasks) -> Task:
        now = datetime.now()
        task = Task(
            id=str(uuid.uuid4()),
            status=TaskStatus.QUEUED,
            scheduled_at=now,
            last_updated=now,
            **request.model_dump()
        )
        self._tasks[task.id] = task
        background_tasks.add_task(self.__start_task, task.id)
        return task

    def __start_task(self, task_id: str):
        if not task_id in self._tasks:
            return

        now = datetime.now()
        task = self._tasks[task_id]
        task.status = TaskStatus.RUNNING
        task.started_at = now
        task.last_updated = now

        def report(message: str):
            task.progress = message
            task.last_updated = datetime.now()

        task_activity = TaskActivity()
        self._activities[task_id] = task_activity
        try:
            with activity.bound(task_activity):
                task.method(report)
            task.status = TaskStatus.COMPLETED
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
        finally:
            self._activities.pop(task_id, None)

        task.last_updated = datetime.now()

    def get_task(self, task_id: str) -> Optional[Task]:
        if task_id not in self._tasks:
            return None

        return self._tasks[task_id]

    def get_activity(self, task_id: str) -> Optional[Activity]:
        """What a RUNNING task is doing right now (working vs. waiting for
        capacity); None for any task that isn't running."""
        task_activity = self._activities.get(task_id)
        if task_activity is None:
            return None
        return task_activity.snapshot(throttled=is_throttled())

    def get_all_tasks(self) -> List[Task]:
        return list(self._tasks.values())

    def delete_task(self, task_id: str) -> Optional[Task]:
        task = self._tasks.pop(task_id, None)
        return task

    def clear_completed_tasks(self) -> List[Task]:
        completed_tasks = [t for t in self._tasks.values() if t.status == TaskStatus.COMPLETED]
        for t in completed_tasks:
            del self._tasks[t.id]

        return completed_tasks
