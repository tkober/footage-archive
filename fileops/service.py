"""Safe move/rename/mkdir service.

Guarantees:
- Disk and DB never drift apart for a single physical rename: either both
  the DB row(s) and the file move together, or neither does.
- Never overwrites or merges into an existing target.
- Every physical ``os.rename`` is journaled (``FileOperations``) before it
  is attempted, so a crash mid-flight can be recovered on next startup
  (see ``recover_pending_operations``).
- ``os.rename`` only — no copy+delete. Cross-filesystem renames (``EXDEV``)
  fail with a clear error instead of silently falling back to a copy.

All filesystem/DB coordination for move, rename, mkdir and preview lives
here; ``api/files.py`` only translates between HTTP and these exceptions.
"""

from __future__ import annotations

import errno
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from db.database import Database, UndoRenameFailedError
from env.environment import Environment
from fileops.pathlocks import PathLockedError, try_exclusive

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class FileOpError(Exception):
    """Base class for all fileops service errors."""


class OutsideRootError(FileOpError):
    """Source or target resolves outside ROOT_DIR."""


class NotFoundError(FileOpError):
    """Source path does not exist."""


class AlreadyExistsError(FileOpError):
    """Target path already exists — never overwritten or merged."""


class InvalidNameError(FileOpError):
    """Name is empty or contains a path separator."""


class SelfMoveError(FileOpError):
    """A directory cannot be moved into itself or a descendant of itself."""


class SidecarConflictError(FileOpError):
    """A sidecar's target path already exists."""


class CrossFilesystemError(FileOpError):
    """os.rename raised EXDEV — source and target are on different filesystems."""


# Re-exported for convenience so API code only needs to import from this module.
__all__ = [
    'FileOpError', 'OutsideRootError', 'NotFoundError', 'AlreadyExistsError',
    'InvalidNameError', 'SelfMoveError', 'SidecarConflictError', 'CrossFilesystemError',
    'PathLockedError', 'MoveResult', 'PreviewResult', 'move_paths', 'rename_path',
    'mkdir', 'preview_move', 'recover_pending_operations',
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class MoveResult:
    path: str
    ok: bool
    new_path: Optional[str] = None
    error: Optional[str] = None


@dataclass
class PreviewResult:
    file_count: int
    tracked_count: int
    sidecars: list[str]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _root() -> Path:
    return Path(Environment().get_root_dir())


def _hidden_extensions() -> set[str]:
    return set(Environment().get_browser_hidden_extensions())


def _validate_name(name: str) -> str:
    name = name.strip()
    if not name or '/' in name or '\\' in name or name in ('.', '..'):
        raise InvalidNameError(f'Invalid name: {name!r}')
    return name


def _resolve_in_root(raw_path: str) -> Path:
    root = _root()
    resolved = Path(raw_path).resolve()
    if not resolved.is_relative_to(root):
        raise OutsideRootError(f'Path outside root directory: {raw_path}')
    return resolved


def _require_exists(path: Path) -> Path:
    if not path.exists():
        raise NotFoundError(f'Path does not exist: {path}')
    return path


def _require_absent(path: Path) -> Path:
    if path.exists():
        raise AlreadyExistsError(f'Target already exists: {path}')
    return path


def _sidecars_for(file_path: Path) -> list[Path]:
    """Files in the same directory sharing the stem, with an extension in
    BROWSER_HIDDEN_EXTENSIONS — these travel along with a file move/rename."""
    hidden = _hidden_extensions()
    if not hidden or not file_path.parent.is_dir():
        return []
    stem = file_path.stem
    result = []
    for entry in file_path.parent.iterdir():
        if entry == file_path or not entry.is_file():
            continue
        if entry.stem == stem and entry.suffix.lower() in hidden:
            result.append(entry)
    return sorted(result)


def _count_files_recursive(directory: Path) -> int:
    total = 0
    for _root_dir, _dirs, files in os.walk(directory):
        total += len(files)
    return total


# ---------------------------------------------------------------------------
# Physical rename primitives (journaled)
# ---------------------------------------------------------------------------

def _os_rename(src: Path, dst: Path) -> None:
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise CrossFilesystemError(
                f'Cannot move "{src}" to "{dst}": source and target are on '
                f'different filesystems — refusing to copy.'
            ) from e
        raise


def _journal_physical_rename(db: Database, kind: str, src: Path, dst: Path,
                             apply_updates) -> None:
    """Insert a 'pending' journal row, then run apply_updates()+os.rename()
    in one DB transaction, finishing the journal row according to outcome.

    ``apply_updates(conn)`` must perform all DB path updates for this single
    physical rename. Raises FileOpError subclasses or re-raises the
    underlying OSError/DB exception on failure.
    """
    op_id = db.insert_file_operation(kind=kind, source_path=str(src), target_path=str(dst))
    state = {'rename_completed': False}

    def do_rename():
        # Re-check immediately before the physical rename: `dst` was free
        # when the caller validated it, but os.rename() on Linux silently
        # *replaces* an existing target, so a target created in the
        # (now very narrow, lock-held) window since would otherwise be
        # clobbered without warning.
        if dst.exists():
            raise AlreadyExistsError(f'Target already exists: {dst}')
        _os_rename(src, dst)
        state['rename_completed'] = True

    def undo_rename():
        os.rename(dst, src)

    try:
        db.run_guarded_rename(apply_updates, do_rename, undo_rename)
    except UndoRenameFailedError:
        # Commit failed AND reversing the physical rename also failed —
        # filesystem/DB may now be inconsistent. Leave the journal row
        # 'pending' (untouched) so recover_pending_operations() reconciles
        # it on next startup, instead of recording a status that would
        # stop recovery from ever looking at it again.
        logger.error('Undo failed after a commit failure for FileOperation '
                     '#%s (%s -> %s); leaving it pending for startup recovery',
                     op_id, src, dst, exc_info=True)
        raise
    except Exception as e:
        # If do_rename() completed, the only way this still raised is a
        # post-rename commit failure, which already called undo_rename().
        status = 'rolled_back' if state['rename_completed'] else 'failed'
        db.mark_file_operation(op_id, status, error=str(e))
        raise
    else:
        db.mark_file_operation(op_id, 'done')


def _rename_single_file_with_sidecars(db: Database, src: Path, dst: Path) -> None:
    """Rename/move one file plus any sidecars, all inside one DB transaction,
    each physical rename getting its own journal row. If a later rename in
    the group fails, already-done renames in the group are reversed before
    re-raising."""
    sidecars = _sidecars_for(src)
    sidecar_targets = [dst.parent / (dst.stem + s.suffix) for s in sidecars]

    for s_target in sidecar_targets:
        if s_target.exists():
            raise SidecarConflictError(f'Sidecar target already exists: {s_target}')

    pairs = [(src, dst)] + list(zip(sidecars, sidecar_targets))
    op_ids = [
        db.insert_file_operation(kind='file_rename', source_path=str(s), target_path=str(d))
        for s, d in pairs
    ]

    done: list[tuple[Path, Path]] = []
    state = {'rename_completed': False}

    def apply_updates(conn):
        for s, d in pairs:
            db.update_file_path_on_conn(conn, str(s.parent), s.name, str(d.parent), d.name)

    def do_rename():
        for s, d in pairs:
            try:
                # Re-check immediately before each physical rename — see
                # the comment in _journal_physical_rename.do_rename for why.
                if d.exists():
                    raise AlreadyExistsError(f'Target already exists: {d}')
                _os_rename(s, d)
            except Exception:
                # Reverse everything already renamed in this group.
                for rs, rd in reversed(done):
                    os.rename(rd, rs)
                done.clear()
                raise
            done.append((s, d))
        state['rename_completed'] = True

    def undo_rename():
        for rs, rd in reversed(done):
            os.rename(rd, rs)
        done.clear()

    try:
        db.run_guarded_rename(apply_updates, do_rename, undo_rename)
    except UndoRenameFailedError:
        # See _journal_physical_rename — leave every row in this group
        # 'pending' so startup recovery can reconcile them individually.
        logger.error('Undo failed after a commit failure for FileOperations '
                     '#%s (group rename of %s); leaving them pending for '
                     'startup recovery', op_ids, src, exc_info=True)
        raise
    except Exception as e:
        # If do_rename() completed fully, the only way run_guarded_rename
        # still raised is a post-rename commit failure, which already
        # invoked undo_rename() to physically reverse every rename.
        status = 'rolled_back' if state['rename_completed'] else 'failed'
        for op_id in op_ids:
            db.mark_file_operation(op_id, status, error=str(e))
        raise
    else:
        for op_id in op_ids:
            db.mark_file_operation(op_id, 'done')


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def mkdir(parent: str, name: str) -> str:
    name = _validate_name(name)
    parent_path = _require_exists(_resolve_in_root(parent))
    if not parent_path.is_dir():
        raise FileOpError(f'Parent is not a directory: {parent_path}')
    new_path = _resolve_in_root(str(parent_path / name))
    _require_absent(new_path)
    new_path.mkdir(parents=False, exist_ok=False)
    return str(new_path)


def rename_path(path: str, new_name: str) -> str:
    """Rename a file or directory in place (same parent). Returns the new path."""
    new_name = _validate_name(new_name)
    src = _require_exists(_resolve_in_root(path))
    dst = _resolve_in_root(str(src.parent / new_name))

    db = Database()
    with try_exclusive([str(src), str(dst)]):
        # Checked inside the lock, right before dispatching — the
        # do_rename() closures re-check again immediately before each
        # physical os.rename, since even the lock doesn't block a target
        # created by something outside this service.
        _require_absent(dst)
        if src.is_dir():
            _rename_or_move_directory(db, src, dst)
        else:
            _rename_single_file_with_sidecars(db, src, dst)
    return str(dst)


def _rename_or_move_directory(db: Database, src: Path, dst: Path) -> None:
    def apply_updates(conn):
        db.update_directory_prefix_on_conn(conn, str(src), str(dst))

    _journal_physical_rename(db, kind='dir_move', src=src, dst=dst, apply_updates=apply_updates)


def preview_move(paths: list[str], target_directory: str) -> PreviewResult:
    db = Database()
    target = _resolve_in_root(target_directory)

    file_count = 0
    tracked_count = 0
    sidecars: list[str] = []

    for raw in paths:
        src = _resolve_in_root(raw)
        if not src.exists():
            continue
        if src.is_dir():
            file_count += _count_files_recursive(src)
            tracked_count += db.count_tracked_files_under(str(src))
        else:
            file_count += 1
            if db.get_file_by_path(str(src)) is not None:
                tracked_count += 1
            for s in _sidecars_for(src):
                sidecars.append(str(s))
                file_count += 1
                if db.get_file_by_path(str(s)) is not None:
                    tracked_count += 1

    # target existing/validity is not enforced for preview — it's informational.
    _ = target
    return PreviewResult(file_count=file_count, tracked_count=tracked_count, sidecars=sidecars)


def move_paths(paths: list[str], target_directory: str) -> list[MoveResult]:
    """Move one or many files, or a single directory, into target_directory.

    A single directory in `paths` is a directory move (one physical
    rename). Otherwise every path is treated as a file and moved
    independently — partial success is allowed, each result reported
    separately.
    """
    if not paths:
        return []

    target = _require_exists(_resolve_in_root(target_directory))
    if not target.is_dir():
        raise FileOpError(f'Target is not a directory: {target}')

    if len(paths) == 1:
        only = _resolve_in_root(paths[0])
        if only.exists() and only.is_dir():
            return [_move_one_directory(only, target)]

    return [_move_one_file(p, target) for p in paths]


def _move_one_directory(src: Path, target_dir: Path) -> MoveResult:
    db = Database()
    dst = target_dir / src.name
    try:
        _require_exists(src)
        dst = _resolve_in_root(str(dst))
        if dst.is_relative_to(src) or dst == src:
            raise SelfMoveError(f'Cannot move "{src}" into itself or a descendant')
        with try_exclusive([str(src), str(dst)]):
            # Checked inside the lock — see rename_path for why this still
            # isn't the last word; do_rename() re-checks right before the
            # physical rename too.
            _require_absent(dst)
            _rename_or_move_directory(db, src, dst)
        return MoveResult(path=str(src), ok=True, new_path=str(dst))
    except (FileOpError, PathLockedError) as e:
        return MoveResult(path=str(src), ok=False, error=str(e))
    except OSError as e:
        # e.g. a PermissionError from the underlying os.rename — don't let
        # one bad item abort the rest of a bulk operation with a 500.
        return MoveResult(path=str(src), ok=False, error=str(e))


def _move_one_file(raw_path: str, target_dir: Path) -> MoveResult:
    db = Database()
    try:
        src = _resolve_in_root(raw_path)
        _require_exists(src)
        if src.is_dir():
            raise FileOpError(f'"{src}" is a directory — move it on its own, not mixed with files')
        dst = _resolve_in_root(str(target_dir / src.name))
        with try_exclusive([str(src), str(dst)]):
            # Checked inside the lock — see rename_path for why this still
            # isn't the last word; do_rename() re-checks right before the
            # physical rename too.
            _require_absent(dst)
            _rename_single_file_with_sidecars(db, src, dst)
        return MoveResult(path=str(src), ok=True, new_path=str(dst))
    except FileOpError as e:
        return MoveResult(path=raw_path, ok=False, error=str(e))
    except PathLockedError as e:
        return MoveResult(path=raw_path, ok=False, error=str(e))
    except OSError as e:
        # e.g. a PermissionError from the underlying os.rename — don't let
        # one bad item abort the rest of a bulk operation with a 500.
        return MoveResult(path=raw_path, ok=False, error=str(e))


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def recover_pending_operations() -> None:
    """Called on startup after `alembic upgrade head`. For every 'pending'
    journal row (left behind by a crash between the journal insert and the
    final status update), figures out which side of the rename actually
    happened on disk and reconciles the DB + journal accordingly."""
    db = Database()
    pending = db.get_pending_file_operations()
    if not pending:
        return

    for row in pending:
        op_id = row['id']
        kind = row['kind']
        source = Path(row['source_path'])
        target = Path(row['target_path'])
        target_exists = target.exists()
        source_exists = source.exists()

        try:
            if target_exists and not source_exists:
                _reconcile_db_for(db, kind, source, target)
                db.mark_file_operation(op_id, 'done',
                                       error='Recovered on startup: target found on disk')
                logger.warning('Recovered pending FileOperation #%s (%s): '
                               'target exists, marked done (%s -> %s)',
                               op_id, kind, source, target)
            elif source_exists and not target_exists:
                _reconcile_db_for(db, kind, target, source)
                db.mark_file_operation(op_id, 'rolled_back',
                                       error='Recovered on startup: source found on disk')
                logger.warning('Recovered pending FileOperation #%s (%s): '
                               'source exists, marked rolled_back (%s -> %s)',
                               op_id, kind, source, target)
            else:
                reason = ('both source and target exist' if source_exists and target_exists
                         else 'neither source nor target exists')
                db.mark_file_operation(op_id, 'failed', error=f'Recovery inconclusive: {reason}')
                logger.warning('Could not recover pending FileOperation #%s (%s): %s (%s -> %s)',
                               op_id, kind, reason, source, target)
        except Exception:
            logger.exception('Failed to recover pending FileOperation #%s (%s -> %s)',
                             op_id, source, target)
            try:
                db.mark_file_operation(op_id, 'failed', error='Recovery raised an exception; see logs')
            except Exception:
                logger.exception('Failed to mark FileOperation #%s as failed during recovery', op_id)


def _reconcile_db_for(db: Database, kind: str, stale_side: Path, live_side: Path) -> None:
    """Idempotently point the DB at `live_side`, given that `stale_side` is
    the path the DB might still reference. Works whether or not the DB was
    already updated — the update is a no-op if nothing matches."""
    if kind == 'dir_move':
        db.update_directory_prefix(str(stale_side), str(live_side))
    else:
        db.update_file_path(str(stale_side.parent), stale_side.name,
                            str(live_side.parent), live_side.name)
