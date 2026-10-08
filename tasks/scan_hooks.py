"""Hooks fired by the scan queue's consumer loop (#138, epic #133) — a small
seam so a later ticket (#139, directory-status rows) can act on a finished
scan unit without touching `tasks/scanqueue.py`'s consumer loop itself.

Currently a no-op; #139 gives `on_unit_done` a real body."""

import logging

logger = logging.getLogger(__name__)


def on_unit_done(directory: str) -> None:
    """Called by `tasks/scanqueue.py::_run_unit` once a ScanUnit finishes
    DONE — never for CANCELLED or FAILED. The caller invokes this
    best-effort (any exception is logged there, never allowed to fail the
    unit itself), so this function is free to do real work later without
    that caller needing a change."""
