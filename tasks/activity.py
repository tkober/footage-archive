"""Per-task activity tracking (#93): is a RUNNING background task actually
doing work right now, or is it only waiting for capacity?

Every background task starts RUNNING as soon as FastAPI hands it a thread, but
the work inside it competes for two shared limits: the worker pool
(`tasks/workerpool.py`) and the heavy-job slots / temperature throttle
(`tasks/loadcontrol.py`). A scan started behind another one therefore sits at
"0 / N" with all its files queued in the pool. This module lets those two
places report, per task, how many of its threads are working and how much of
its work is waiting, so the task list can tell "running" from "waiting".

The task's own thread is bound to its `TaskActivity` by the TaskManager;
`parallel_map` carries the binding over to the pool threads that run the
task's items. Code running outside any task (e.g. an interactive render from a
request) sees `current()` as None and every hook is a no-op.
"""

import threading
from contextlib import contextmanager
from enum import Enum
from typing import Optional

_local = threading.local()


class Activity(str, Enum):
    ACTIVE = 'ACTIVE'
    WAITING_WORKER = 'WAITING_WORKER'  # items queued behind other work in the shared pool
    WAITING_HEAVY = 'WAITING_HEAVY'  # every heavy-job slot is taken
    THROTTLED = 'THROTTLED'  # heavy work paused: host too hot / loaded


class TaskActivity:
    """Thread-safe counters for one task. `working` counts threads currently
    executing the task's code (not blocked on a pool/heavy-slot wait)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.working = 0
        self.pool_pending = 0
        self.heavy_waiting = 0

    def _add(self, working: int = 0, pool_pending: int = 0, heavy_waiting: int = 0):
        with self._lock:
            self.working += working
            self.pool_pending += pool_pending
            self.heavy_waiting += heavy_waiting

    def snapshot(self, throttled: bool) -> Activity:
        """Classify the task right now. `throttled` is loadcontrol's global
        throttle flag — it decides whether a heavy-slot wait is a pause."""
        with self._lock:
            working, pool_pending, heavy_waiting = self.working, self.pool_pending, self.heavy_waiting
        if working > 0:
            return Activity.ACTIVE
        if heavy_waiting > 0:
            return Activity.THROTTLED if throttled else Activity.WAITING_HEAVY
        if pool_pending > 0:
            return Activity.WAITING_WORKER
        # Between two steps (e.g. a pool batch just finished): still running.
        return Activity.ACTIVE


def current() -> Optional[TaskActivity]:
    return getattr(_local, 'activity', None)


@contextmanager
def bound(activity: Optional[TaskActivity]):
    """Run the block as a working thread of `activity` (no-op for None)."""
    previous = current()
    _local.activity = activity
    if activity is not None:
        activity._add(working=1)
    try:
        yield
    finally:
        if activity is not None:
            activity._add(working=-1)
        _local.activity = previous


@contextmanager
def idle():
    """The current thread is blocked on its own pool items finishing — not
    working itself, but not a capacity wait either."""
    activity = current()
    if activity is not None:
        activity._add(working=-1)
    try:
        yield
    finally:
        if activity is not None:
            activity._add(working=1)


@contextmanager
def waiting_for_heavy_slot():
    """The current thread waits for a heavy-job slot or the throttle."""
    activity = current()
    if activity is not None:
        activity._add(working=-1, heavy_waiting=1)
    try:
        yield
    finally:
        if activity is not None:
            activity._add(working=1, heavy_waiting=-1)


def submitted(activity: Optional[TaskActivity]):
    """One item of `activity` was queued in the worker pool."""
    if activity is not None:
        activity._add(pool_pending=1)


@contextmanager
def started(activity: Optional[TaskActivity]):
    """A pool thread picked up one of `activity`'s queued items."""
    if activity is not None:
        activity._add(pool_pending=-1)
    with bound(activity):
        yield
