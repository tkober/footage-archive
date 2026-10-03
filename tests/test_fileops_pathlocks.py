"""Unit tests for fileops/pathlocks.py — pure in-memory, no DB or filesystem
needed (paths don't have to exist; is_relative_to/overlap logic only cares
about the string structure after Path.resolve(), which works on nonexistent
paths too)."""

import threading
import time

import pytest

from fileops.pathlocks import PathLockedError, shared, try_exclusive


def test_try_exclusive_succeeds_when_nothing_else_holds_a_lock(tmp_path):
    p = tmp_path / 'a'
    with try_exclusive([str(p)]):
        pass  # no exception


def test_try_exclusive_rejects_overlap_with_active_exclusive_lock(tmp_path):
    p = tmp_path / 'a'
    with try_exclusive([str(p)]):
        with pytest.raises(PathLockedError):
            with try_exclusive([str(p)]):
                pass


def test_try_exclusive_rejects_when_ancestor_is_exclusively_locked(tmp_path):
    parent = tmp_path / 'a'
    child = parent / 'b'
    with try_exclusive([str(parent)]):
        with pytest.raises(PathLockedError):
            with try_exclusive([str(child)]):
                pass


def test_try_exclusive_rejects_when_descendant_is_exclusively_locked(tmp_path):
    parent = tmp_path / 'a'
    child = parent / 'b'
    with try_exclusive([str(child)]):
        with pytest.raises(PathLockedError):
            with try_exclusive([str(parent)]):
                pass


def test_try_exclusive_allows_unrelated_siblings(tmp_path):
    a = tmp_path / 'a'
    b = tmp_path / 'b'
    with try_exclusive([str(a)]):
        with try_exclusive([str(b)]):
            pass  # no exception — siblings don't overlap


def test_sibling_prefix_does_not_overlap_like_string_startswith_would():
    """/a/b vs /a/bc must NOT be treated as overlapping — this is exactly the
    bug Path.is_relative_to (vs plain string startswith) protects against."""
    with try_exclusive(['/tmp/pathlocks-test/a/b']):
        with try_exclusive(['/tmp/pathlocks-test/a/bc']):
            pass  # no exception


def test_try_exclusive_rejects_overlap_with_active_shared_lock(tmp_path):
    p = tmp_path / 'a'
    release = threading.Event()
    entered = threading.Event()

    def hold_shared():
        with shared(str(p)):
            entered.set()
            release.wait(timeout=2)

    t = threading.Thread(target=hold_shared)
    t.start()
    try:
        assert entered.wait(timeout=2)
        with pytest.raises(PathLockedError):
            with try_exclusive([str(p)]):
                pass
    finally:
        release.set()
        t.join(timeout=2)


def test_shared_blocks_while_exclusive_lock_is_held_then_proceeds(tmp_path):
    p = tmp_path / 'a'
    order = []

    with try_exclusive([str(p)]):
        def take_shared():
            with shared(str(p)):
                order.append('shared-acquired')

        t = threading.Thread(target=take_shared)
        t.start()
        time.sleep(0.1)
        assert order == []  # still blocked while we hold the exclusive lock
        order.append('about-to-release-exclusive')

    t.join(timeout=2)
    assert order == ['about-to-release-exclusive', 'shared-acquired']


def test_multiple_shared_locks_on_same_path_can_coexist(tmp_path):
    p = tmp_path / 'a'
    with shared(str(p)):
        with shared(str(p)):
            pass  # no exception — shared locks don't block each other
