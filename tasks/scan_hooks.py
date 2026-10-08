"""Hooks fired by the scan queue's consumer loop (#138, epic #133) — a small
seam so a later ticket (#139, directory-status rows) can act on a finished
scan unit without touching `tasks/scanqueue.py`'s consumer loop itself.

#139: `on_unit_done` refreshes the unit's directory and every ancestor up to
ROOT_DIR (`tasks/directory_stats.py::refresh_chain`), so the browser's
folder-tile badges reflect a scan without any recursive work of their own."""

import logging

from tasks import directory_stats

logger = logging.getLogger(__name__)


def on_unit_done(directory: str) -> None:
    """Called by `tasks/scanqueue.py::_run_unit` once a ScanUnit finishes
    DONE — never for CANCELLED or FAILED. The caller invokes this
    best-effort (any exception is logged there, never allowed to fail the
    unit itself), so this function is free to do real work here."""
    directory_stats.refresh_chain(directory, 'scan')
