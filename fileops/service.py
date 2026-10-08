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
from datetime import datetime
from pathlib import Path
from typing import Optional

from db.database import Database, UndoRenameFailedError
from env.environment import Environment
from env.hidden_files import is_hidden_system_file, is_system_junk_name
from fileops.pathlocks import PathLockedError, try_exclusive
from fileops.trash import ensure_trash_dir, is_in_trash
from tasks import directory_stats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DirectoryStats hooks (#139) — every physical move/rename/trash/mkdir below
# calls one of these once its own DB transaction has already committed.
# DirectoryStats is a derived cache, not a source of truth like Files, so
# these never need to share that transaction, and never fail the operation
# itself — any exception here is logged and swallowed.
# ---------------------------------------------------------------------------

def _refresh_directory_stats_chain(directory: str) -> None:
    try:
        directory_stats.refresh_chain(directory, 'fileops')
    except Exception:
        logger.exception('Failed to refresh directory status for %s after a file operation', directory)


def _reprefix_directory_stats(old_directory: str, new_directory: str) -> None:
    try:
        Database().reprefix_directory_stats(old_directory, new_directory)
    except Exception:
        logger.exception('Failed to re-prefix directory status rows from %s to %s', old_directory, new_directory)


def _delete_directory_stats_subtree(directory: str) -> None:
    try:
        Database().delete_directory_stats_subtree(directory)
    except Exception:
        logger.exception('Failed to delete directory status rows under %s', directory)


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


class TrashPathError(FileOpError):
    """Source or target is the trash directory itself or something inside it."""


# Re-exported for convenience so API code only needs to import from this module.
__all__ = [
    'FileOpError', 'OutsideRootError', 'NotFoundError', 'AlreadyExistsError',
    'InvalidNameError', 'SelfMoveError', 'SidecarConflictError', 'CrossFilesystemError',
    'TrashPathError', 'PathLockedError', 'MoveResult', 'PreviewResult', 'move_paths',
    'rename_path', 'mkdir', 'preview_move', 'recover_pending_operations',
    'DeletePreviewResult', 'DeleteResult', 'DeleteBatchResult', 'preview_delete', 'delete_paths',
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


def _hidden_names() -> set[str]:
    return set(Environment().get_browser_hidden_names())


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


def _require_not_trash(path: Path) -> Path:
    if is_in_trash(path):
        raise TrashPathError(f'Path is the trash directory or inside it: {path}')
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
    """Recursive file count used by preview_move/preview_delete and the
    trashed-directory untracked-count in _delete_one. Excludes hidden system
    files (#81: ._* AppleDouble sidecars, BROWSER_HIDDEN_NAMES, and
    BROWSER_HIDDEN_EXTENSIONS sidecars) — a folder containing only a
    .DS_Store shouldn't inflate a move/delete's reported file count."""
    hidden_extensions = _hidden_extensions()
    hidden_names = _hidden_names()
    total = 0
    for _root_dir, _dirs, files in os.walk(directory):
        total += sum(1 for f in files if not is_hidden_system_file(f, hidden_extensions, hidden_names))
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


def _group_rename(db: Database, kind: str, pairs: list[tuple[Path, Path]], apply_updates) -> None:
    """Journal + physically perform one or more ``os.rename``s as a single
    logical operation, all inside one DB transaction (``apply_updates(conn)``
    must perform every DB-side change for the whole group). Each physical
    rename gets its own ``FileOperations`` row (same ``kind``), so startup
    recovery can reconcile them individually. If a later rename in the group
    fails, every rename already done earlier in the group is reversed before
    re-raising — the group either fully lands or fully doesn't.

    Used for a single physical rename (one pair — dir move/rename, dir/file
    trash) as well as a file-plus-sidecars group (several pairs — file
    move/rename, file trash)."""
    op_ids = [
        db.insert_file_operation(kind=kind, source_path=str(s), target_path=str(d))
        for s, d in pairs
    ]

    done: list[tuple[Path, Path]] = []
    state = {'rename_completed': False}

    def do_rename():
        for s, d in pairs:
            try:
                # Re-check immediately before each physical rename: targets
                # were free when the caller validated them, but os.rename()
                # on Linux silently *replaces* an existing target, so one
                # created in the (now very narrow, lock-held) window since
                # would otherwise be clobbered without warning.
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
        # Commit failed AND reversing the physical rename(s) also failed —
        # filesystem/DB may now be inconsistent. Leave every journal row in
        # this group 'pending' (untouched) so recover_pending_operations()
        # reconciles them individually on next startup, instead of
        # recording a status that would stop recovery from ever looking at
        # them again.
        logger.error('Undo failed after a commit failure for FileOperations '
                     '#%s (group rename, kind=%s); leaving them pending for '
                     'startup recovery', op_ids, kind, exc_info=True)
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


def _journal_physical_rename(db: Database, kind: str, src: Path, dst: Path,
                             apply_updates) -> None:
    """Single-pair convenience wrapper around _group_rename (dir move/rename)."""
    _group_rename(db, kind, [(src, dst)], apply_updates)


def _rename_single_file_with_sidecars(db: Database, src: Path, dst: Path) -> None:
    """Rename/move one file plus any sidecars, all inside one DB transaction
    via _group_rename (``kind='file_rename'``)."""
    sidecars = _sidecars_for(src)
    sidecar_targets = [dst.parent / (dst.stem + s.suffix) for s in sidecars]

    for s_target in sidecar_targets:
        if s_target.exists():
            raise SidecarConflictError(f'Sidecar target already exists: {s_target}')

    pairs = [(src, dst)] + list(zip(sidecars, sidecar_targets))

    def apply_updates(conn):
        for s, d in pairs:
            db.update_file_path_on_conn(conn, str(s.parent), s.name, str(d.parent), d.name)

    _group_rename(db, kind='file_rename', pairs=pairs, apply_updates=apply_updates)


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def mkdir(parent: str, name: str) -> str:
    name = _validate_name(name)
    parent_path = _require_exists(_resolve_in_root(parent))
    _require_not_trash(parent_path)
    if not parent_path.is_dir():
        raise FileOpError(f'Parent is not a directory: {parent_path}')
    new_path = _resolve_in_root(str(parent_path / name))
    _require_not_trash(new_path)
    _require_absent(new_path)
    new_path.mkdir(parents=False, exist_ok=False)
    # #139: an empty, complete row for the new directory — otherwise its
    # parent would read as incomplete (an unknown subdirectory) until
    # something else happens to refresh it.
    _refresh_directory_stats_chain(str(new_path))
    return str(new_path)


def rename_path(path: str, new_name: str) -> str:
    """Rename a file or directory in place (same parent). Returns the new path."""
    new_name = _validate_name(new_name)
    src = _require_exists(_resolve_in_root(path))
    _require_not_trash(src)
    dst = _resolve_in_root(str(src.parent / new_name))
    _require_not_trash(dst)

    db = Database()
    with try_exclusive([str(src), str(dst)]):
        # Checked inside the lock, right before dispatching — the
        # do_rename() closures re-check again immediately before each
        # physical os.rename, since even the lock doesn't block a target
        # created by something outside this service.
        _require_absent(dst)
        is_dir = src.is_dir()
        if is_dir:
            _rename_or_move_directory(db, src, dst)
        else:
            _rename_single_file_with_sidecars(db, src, dst)
    # #139: a directory rename re-prefixes its subtree's rows (both
    # directory and parent columns) before refreshing both parent chains;
    # a file rename only ever needs the chain(s) refreshed — renaming a
    # file never moves a DirectoryStats row.
    if is_dir:
        _reprefix_directory_stats(str(src), str(dst))
    _refresh_directory_stats_chain(str(src.parent))
    if dst.parent != src.parent:
        _refresh_directory_stats_chain(str(dst.parent))
    return str(dst)


def _rename_or_move_directory(db: Database, src: Path, dst: Path) -> None:
    def apply_updates(conn):
        db.update_directory_prefix_on_conn(conn, str(src), str(dst))

    _journal_physical_rename(db, kind='dir_move', src=src, dst=dst, apply_updates=apply_updates)


def preview_move(paths: list[str], target_directory: str) -> PreviewResult:
    db = Database()
    target = _resolve_in_root(target_directory)
    _require_not_trash(target)

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
    _require_not_trash(target)
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
        _require_not_trash(src)
        dst = _resolve_in_root(str(dst))
        if dst.is_relative_to(src) or dst == src:
            raise SelfMoveError(f'Cannot move "{src}" into itself or a descendant')
        with try_exclusive([str(src), str(dst)]):
            # Checked inside the lock — see rename_path for why this still
            # isn't the last word; do_rename() re-checks right before the
            # physical rename too.
            _require_absent(dst)
            _rename_or_move_directory(db, src, dst)
        _reprefix_directory_stats(str(src), str(dst))  # #139
        _refresh_directory_stats_chain(str(src.parent))
        _refresh_directory_stats_chain(str(dst.parent))
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
        _require_not_trash(src)
        if src.is_dir():
            raise FileOpError(f'"{src}" is a directory — move it on its own, not mixed with files')
        dst = _resolve_in_root(str(target_dir / src.name))
        with try_exclusive([str(src), str(dst)]):
            # Checked inside the lock — see rename_path for why this still
            # isn't the last word; do_rename() re-checks right before the
            # physical rename too.
            _require_absent(dst)
            _rename_single_file_with_sidecars(db, src, dst)
        _refresh_directory_stats_chain(str(src.parent))  # #139
        _refresh_directory_stats_chain(str(dst.parent))
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
# Delete to trash
# ---------------------------------------------------------------------------

@dataclass
class DeletePreviewResult:
    file_count: int
    tracked_count: int
    sidecars: list[str]
    list_item_count: int
    keyword_count: int


@dataclass
class DeleteResult:
    path: str
    ok: bool
    trash_path: Optional[str] = None
    untracked_count: Optional[int] = None
    error: Optional[str] = None


@dataclass
class DeleteBatchResult:
    trash_batch: str
    results: list[DeleteResult]


def preview_delete(paths: list[str]) -> DeletePreviewResult:
    """Dry-run counts for a delete-to-trash batch: how many files are
    involved (recursive for directories, including sidecars), how many of
    those are tracked, and how much tracking data would be lost (ListItems/
    FileKeywords rows for the affected hashes) — so the frontend can warn
    before the user confirms."""
    db = Database()

    file_count = 0
    tracked_count = 0
    sidecars: list[str] = []
    list_item_count = 0
    keyword_count = 0

    for raw in paths:
        try:
            src = _resolve_in_root(raw)
        except FileOpError:
            continue
        if not src.exists():
            continue
        if src.is_dir():
            file_count += _count_files_recursive(src)
            rows = db.get_tracked_files_with_attachment_counts(directory=str(src))
            tracked_count += len(rows)
            keyword_count += sum(r['keyword_count'] for r in rows)
            list_item_count += sum(r['list_count'] for r in rows)
        else:
            file_count += 1
            hashes = []
            main_rec = db.get_file_by_path(str(src))
            if main_rec is not None:
                tracked_count += 1
                hashes.append(main_rec['md5_hash'])
            for s in _sidecars_for(src):
                sidecars.append(str(s))
                file_count += 1
                s_rec = db.get_file_by_path(str(s))
                if s_rec is not None:
                    tracked_count += 1
                    hashes.append(s_rec['md5_hash'])
            if hashes:
                summaries = db.get_tracked_files_with_attachment_counts(md5_hashes=hashes)
                keyword_count += sum(r['keyword_count'] for r in summaries)
                list_item_count += sum(r['list_count'] for r in summaries)

    return DeletePreviewResult(file_count=file_count, tracked_count=tracked_count,
                               sidecars=sidecars, list_item_count=list_item_count,
                               keyword_count=keyword_count)


def _make_batch_dir() -> Path:
    """TRASH/<YYYY-MM-DD_HHMMSS>[_n] — one per delete_paths() call, shared by
    every item in that call. `_2`, `_3`, … is appended if the plain timestamp
    already exists (e.g. two deletes within the same second)."""
    trash_dir = ensure_trash_dir()
    timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    candidate = trash_dir / timestamp
    suffix = 2
    while candidate.exists():
        candidate = trash_dir / f'{timestamp}_{suffix}'
        suffix += 1
    candidate.mkdir(parents=False, exist_ok=False)
    return candidate


def _prune_empty_dirs(directory: Path) -> None:
    """Remove `directory` and any empty subdirectory left behind by a
    partially/wholly failed batch (e.g. a target's parent dirs were created
    but the rename itself never happened).

    A directory whose only entries are system junk (#81: ._* AppleDouble
    sidecars or a BROWSER_HIDDEN_NAMES match — never a BROWSER_HIDDEN_EXTENSIONS
    sidecar like .xmp, which is real companion data) counts as empty too:
    those junk files are deleted, then the directory itself. os.walk with
    topdown=False visits children first, so by the time a parent is checked
    any subdirectory that was itself prunable is already gone — a remaining
    subdirectory correctly keeps the parent from being treated as empty."""
    if not directory.exists():
        return
    hidden_names = _hidden_names()
    for dirpath, _dirnames, _filenames in os.walk(directory, topdown=False):
        p = Path(dirpath)
        try:
            entries = list(p.iterdir())
            if not entries:
                p.rmdir()
                continue
            if all(e.is_file() and is_system_junk_name(e.name, hidden_names) for e in entries):
                for e in entries:
                    e.unlink()
                p.rmdir()
        except OSError:
            pass


def _trash_single_file_with_sidecars(db: Database, src: Path, dst: Path) -> None:
    """Rename one file plus any sidecars into the trash, deleting their
    tracking (if any) in the same DB transaction as the physical rename(s) —
    mirrors _rename_single_file_with_sidecars, but the apply_updates side
    deletes tracking instead of relocating it."""
    sidecars = _sidecars_for(src)
    sidecar_targets = [dst.parent / (dst.stem + s.suffix) for s in sidecars]

    for s_target in sidecar_targets:
        if s_target.exists():
            raise SidecarConflictError(f'Sidecar target already exists: {s_target}')

    pairs = [(src, dst)] + list(zip(sidecars, sidecar_targets))

    def apply_updates(conn):
        hashes = []
        paths = []
        for s, _d in pairs:
            paths.append(str(s))
            md5 = db.get_hash_by_path_on_conn(conn, str(s.parent), s.name)
            if md5:
                hashes.append(md5)
        db.delete_files_on_conn(conn, hashes)
        db.delete_path_conflicts_by_candidate_paths_on_conn(conn, paths)

    _group_rename(db, kind='file_trash', pairs=pairs, apply_updates=apply_updates)


def _trash_one_directory(db: Database, src: Path, dst: Path) -> None:
    """Single physical rename of a directory into the trash, deleting every
    Files row at or under it (plus dependents and PathConflicts) in the same
    DB transaction — mirrors _rename_or_move_directory."""
    def apply_updates(conn):
        hashes = db.get_hashes_under_directory_on_conn(conn, str(src))
        db.delete_files_on_conn(conn, hashes)
        db.delete_path_conflicts_under_on_conn(conn, str(src))

    _group_rename(db, kind='dir_trash', pairs=[(src, dst)], apply_updates=apply_updates)


def _delete_one(raw_path: str, batch_dir: Path) -> DeleteResult:
    db = Database()
    try:
        root = _root()
        src = _resolve_in_root(raw_path)
        _require_exists(src)
        if src == root:
            raise FileOpError('Cannot delete ROOT_DIR itself')
        _require_not_trash(src)

        rel = src.relative_to(root)
        dst = batch_dir / rel

        with try_exclusive([str(src), str(dst)]):
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Checked inside the lock — see rename_path for why this still
            # isn't the last word; do_rename() re-checks right before the
            # physical rename too.
            _require_absent(dst)
            if src.is_dir():
                total = _count_files_recursive(src)
                tracked = db.count_tracked_files_under(str(src))
                _trash_one_directory(db, src, dst)
                _delete_directory_stats_subtree(str(src))  # #139 — gone for good, never reconciled
                _refresh_directory_stats_chain(str(src.parent))
                return DeleteResult(path=raw_path, ok=True, trash_path=str(dst),
                                    untracked_count=total - tracked)
            else:
                _trash_single_file_with_sidecars(db, src, dst)
                _refresh_directory_stats_chain(str(src.parent))  # #139
                return DeleteResult(path=raw_path, ok=True, trash_path=str(dst))
    except FileOpError as e:
        return DeleteResult(path=raw_path, ok=False, error=str(e))
    except PathLockedError as e:
        return DeleteResult(path=raw_path, ok=False, error=str(e))
    except OSError as e:
        # e.g. a PermissionError from the underlying os.rename — don't let
        # one bad item abort the rest of a bulk operation with a 500.
        return DeleteResult(path=raw_path, ok=False, error=str(e))


def delete_paths(paths: list[str]) -> DeleteBatchResult:
    """Move each of ``paths`` (files and/or directories, independently) into
    one shared trash batch directory, deleting their tracking in the same DB
    transaction as each physical rename. Partial success is allowed — every
    item gets its own result. A path already removed because it sat under an
    earlier, successfully-trashed directory in the same batch simply reports
    NotFoundError (its original location no longer exists)."""
    if not paths:
        return DeleteBatchResult(trash_batch='', results=[])

    batch_dir = _make_batch_dir()
    results = [_delete_one(p, batch_dir) for p in paths]

    # Nothing landed (every item failed, or failed after only creating empty
    # intermediate parent dirs) — don't leave an empty batch folder behind.
    _prune_empty_dirs(batch_dir)

    return DeleteBatchResult(trash_batch=str(batch_dir), results=results)


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
                if kind in ('file_trash', 'dir_trash'):
                    # The physical rename into the trash happened, but the DB
                    # side of that same transaction never committed before
                    # the crash — finish it now. Idempotent: safe even if the
                    # DB side had in fact already committed.
                    db.finish_pending_trash_delete(kind, str(source))
                else:
                    _reconcile_db_for(db, kind, source, target)
                db.mark_file_operation(op_id, 'done',
                                       error='Recovered on startup: target found on disk')
                logger.warning('Recovered pending FileOperation #%s (%s): '
                               'target exists, marked done (%s -> %s)',
                               op_id, kind, source, target)
            elif source_exists and not target_exists:
                if kind in ('file_trash', 'dir_trash'):
                    # The physical rename never happened, so the one
                    # transaction wrapping it never committed either — the
                    # DB was never touched; nothing to reconcile. (Calling
                    # the move/rename path-rewrite here would be wrong: it
                    # would rewrite paths for an operation that was never a
                    # path *rename*.)
                    pass
                else:
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
