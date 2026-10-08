import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, StrictStr

from env.environment import Environment
from env.hidden_files import is_hidden_system_file
from tasks.workerpool import parallel_map


class ScanResult(BaseModel):
    md5_hash: StrictStr
    file_name: StrictStr
    file_extension: StrictStr
    media_type: StrictStr | None
    directory: StrictStr
    last_indexed_at: datetime


class Scanner:
    __block_size: int

    def __init__(self, block_size: int = 1024 * 1024):
        self.__block_size = block_size

    def scan_directory(self, path: Path) -> [ScanResult]:
        return self.scan_files(path.rglob('*'))

    def scan_files(self, files: [Path]) -> [ScanResult]:
        """Facade kept behaviour-identical for every caller that still wants
        the whole input collected and hashed in one go (rediscover's
        internals aside, the checksum endpoint, the DaVinci import, tests):
        a single bad file aborts the lot via `parallel_map`, exactly as
        before #135. The streaming scan (`api/tracking.py::_index_candidates`)
        calls `collect_candidates`/`hash_candidates` directly instead, batch
        by batch, with its own failure isolation and progress reporting."""
        candidates = self.collect_candidates(files)
        return self.hash_candidates(candidates)

    def collect_candidates(self, files: [Path]) -> list[Path]:
        """Filter `files` down to the paths a scan actually hashes: no
        hidden system junk (#81), nothing inside the trash, a regular
        existing file whose extension is configured for scanning. Split out
        of `scan_files` (#135) so a streaming caller can collect once for
        the whole tree and then hash/reconcile it batch by batch."""
        env = Environment()
        considered_file_extensions = env.get_scanning_file_extensions()
        hidden_extensions = set(env.get_browser_hidden_extensions())
        hidden_names = set(env.get_browser_hidden_names())
        # Resolved once per scan: resolving every walked path would cost
        # extra syscalls per file on the NAS, so files are compared by abspath.
        trash_dir = env.get_trash_dir().resolve()

        candidates = []
        for f in files:
            f_path = Path(f)
            # System files (.DS_Store, Thumbs.db, desktop.ini, AppleDouble
            # ._* sidecars, #81) are never tracked. Their extension isn't in
            # considered_file_extensions anyway, but the name check (._*,
            # BROWSER_HIDDEN_NAMES) needs this explicit skip, and routing it
            # through the shared predicate keeps the rule in one place.
            if is_hidden_system_file(f_path.name, hidden_extensions, hidden_names):
                continue
            if Path(os.path.abspath(f_path)).is_relative_to(trash_dir):  # never (re)track anything inside the trash
                continue
            if not f_path.is_dir() and f_path.exists() and f_path.suffix.lower() in considered_file_extensions:
                candidates.append(f_path)
        return candidates

    def hash_candidates(self, candidates: list[Path],
                         progress: Optional[Callable[[Path, bool], None]] = None,
                         isolate_errors: bool = False) -> list[ScanResult]:
        """Hash every candidate, fanned out across the shared worker pool
        (I/O bound — reading whole files, often large videos over the
        network); order of the *input* is preserved internally, but since a
        failing candidate can be dropped (see `isolate_errors`), the result
        is a plain list, not zippable against `candidates` by index.

        `isolate_errors=False` (the default — used by the `scan_files`/
        `scan_directory` facade and by `rediscover_directory`, which needs
        the complete hash set to classify unambiguously) keeps today's
        behaviour: any exception propagates out through `parallel_map` and
        aborts the whole call, same as before #135. `isolate_errors=True`
        (the streaming scan, `api/tracking.py::_index_candidates`) instead
        logs and skips a per-file `OSError` (vanished file, permission,
        I/O) — that candidate is left out of the result, and the rest keep
        hashing; the caller can tell how many failed from
        `len(candidates) - len(result)`.

        `progress`, if given, is called once per candidate — `(path, ok)` —
        right after its own hash attempt, success or failure. This lets a
        caller report a monotonic 'Hashed x / y' progress across more than
        just this one call (e.g. one shared counter spanning every batch of
        a streaming scan)."""
        media_type_map = Environment().get_media_type_map()
        indexed_at = datetime.now()

        def do_hash(f_path: Path) -> Optional[ScanResult]:
            try:
                md5_hash = self.md5_hash(str(f_path))
            except OSError:
                if not isolate_errors:
                    raise
                logging.exception(f'Failed to hash {f_path}')
                if progress:
                    progress(f_path, False)
                return None
            if progress:
                progress(f_path, True)
            return ScanResult(
                md5_hash=md5_hash,
                file_name=f_path.name,
                file_extension=f_path.suffix,
                media_type=media_type_map.get(f_path.suffix.lower()),
                directory=str(f_path.parent),
                last_indexed_at=indexed_at,
            )

        results = parallel_map(candidates, do_hash)
        return [r for r in results if r is not None]

    def md5_hash(self, path):
        hasher = hashlib.md5()
        with open(path, "rb") as file:
            for block in iter(lambda: file.read(self.__block_size), b""):
                hasher.update(block)
        return hasher.hexdigest()
