"""Unit tests for tasks/activity.py (#93): a RUNNING task reports whether it is
working or waiting for the worker pool / a heavy-job slot / the throttle."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tasks import activity, loadcontrol, workerpool
from tasks.activity import Activity, TaskActivity


@pytest.fixture(autouse=True)
def _isolated_limits(monkeypatch):
    """A single-thread pool and a single heavy slot, with no throttle by
    default, so the tests decide exactly who has to wait."""
    monkeypatch.setattr(workerpool, '_pool', ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(loadcontrol, '_semaphore', threading.BoundedSemaphore(1))
    monkeypatch.setattr(loadcontrol, '_throttle_reason', lambda: None)
    yield
    workerpool._pool.shutdown(wait=True)


def _run_in_task(target) -> tuple[TaskActivity, threading.Thread]:
    """Run `target` on its own thread bound to a fresh TaskActivity, the way
    TaskManager runs a background task."""
    task_activity = TaskActivity()

    def body():
        with activity.bound(task_activity):
            target()

    thread = threading.Thread(target=body)
    thread.start()
    return task_activity, thread


def test_working_task_is_active():
    release = threading.Event()
    task_activity, thread = _run_in_task(lambda: release.wait(5))
    try:
        assert task_activity.snapshot(throttled=False) == Activity.ACTIVE
    finally:
        release.set()
        thread.join(5)
    assert task_activity.working == 0


def test_items_queued_behind_another_task_wait_for_worker():
    blocker_started, release_blocker = threading.Event(), threading.Event()

    def block(_):
        blocker_started.set()
        release_blocker.wait(5)

    first, first_thread = _run_in_task(lambda: workerpool.parallel_map([1], block))
    assert blocker_started.wait(5)

    second, second_thread = _run_in_task(lambda: workerpool.parallel_map([1, 2, 3], lambda x: x))
    try:
        _wait_until(lambda: second.pool_pending == 3)
        assert first.snapshot(throttled=False) == Activity.ACTIVE
        assert second.snapshot(throttled=False) == Activity.WAITING_WORKER
    finally:
        release_blocker.set()
        first_thread.join(5)
        second_thread.join(5)

    assert (second.working, second.pool_pending, second.heavy_waiting) == (0, 0, 0)


def test_heavy_slot_wait_and_throttle():
    holder_in, release_holder = threading.Event(), threading.Event()

    def hold():
        with loadcontrol.heavy_slot('holder'):
            holder_in.set()
            release_holder.wait(5)

    holder_thread = threading.Thread(target=hold)
    holder_thread.start()
    assert holder_in.wait(5)

    def wants_slot():
        with loadcontrol.heavy_slot('waiter'):
            pass

    task_activity, thread = _run_in_task(wants_slot)
    try:
        _wait_until(lambda: task_activity.heavy_waiting == 1)
        assert task_activity.snapshot(throttled=False) == Activity.WAITING_HEAVY
        assert task_activity.snapshot(throttled=True) == Activity.THROTTLED
    finally:
        release_holder.set()
        holder_thread.join(5)
        thread.join(5)

    assert (task_activity.working, task_activity.heavy_waiting) == (0, 0)


def test_hooks_are_noops_outside_a_task():
    assert activity.current() is None
    assert workerpool.parallel_map([1, 2], lambda x: x * 2) == [2, 4]
    with loadcontrol.heavy_slot('no task'):
        pass


def _wait_until(condition, timeout=5.0):
    done = threading.Event()
    deadline = threading.Timer(timeout, done.set)
    deadline.start()
    try:
        while not condition():
            if done.wait(0.01):
                raise AssertionError('condition not reached in time')
    finally:
        deadline.cancel()
