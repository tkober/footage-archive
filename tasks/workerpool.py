from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Callable, Iterable, TypeVar

from env.environment import Environment

T = TypeVar('T')
R = TypeVar('R')

_pool: ThreadPoolExecutor | None = None
_pool_lock = Lock()


def get_worker_pool() -> ThreadPoolExecutor:
    """Process-wide bounded thread pool shared by every background job.

    A single fixed-size pool decouples the (unbounded) number of scheduled jobs
    from the amount of work running at once: no matter how many scans are queued,
    at most WORKER_POOL_SIZE files are probed/hashed concurrently. That ceiling
    also keeps concurrent DB connections under the engine pool's limit.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                size = Environment().get_worker_pool_size()
                _pool = ThreadPoolExecutor(max_workers=size, thread_name_prefix='worker')
    return _pool


def parallel_map(items: Iterable[T], fn: Callable[[T], R]) -> list[R]:
    """Run fn over items on the shared worker pool, returning results in input order.

    Exceptions raised by fn propagate to the caller (first one wins). Callers that
    need per-item failure isolation should swallow exceptions inside fn.
    """
    items = list(items)
    if not items:
        return []
    results: list[R] = [None] * len(items)  # type: ignore[list-item]
    pool = get_worker_pool()
    futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
    for future in as_completed(futures):
        results[futures[future]] = future.result()
    return results
