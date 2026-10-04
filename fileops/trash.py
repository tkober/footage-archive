"""Central helper for hiding the trash directory from the rest of the app.

The trash lives inside ROOT_DIR (see ``Environment.get_trash_dir()``) so
moving into it is always a same-filesystem ``os.rename``. It must never be
browsable, scanned, rediscovered, or a valid move/rename target.
"""

from __future__ import annotations

from pathlib import Path

from env.environment import Environment


def is_in_trash(path: Path | str) -> bool:
    """True if ``path`` is the trash directory itself or anything under it.

    Always compares *resolved* paths via ``Path.is_relative_to`` — never
    string ``startswith`` — so a sibling like ``.trash-old`` is never
    mistaken for the trash.
    """
    resolved = Path(path).resolve()
    trash_dir = Environment().get_trash_dir().resolve()
    return resolved == trash_dir or resolved.is_relative_to(trash_dir)


def ensure_trash_dir() -> Path:
    """Create the trash directory if it doesn't exist yet and return it."""
    trash_dir = Environment().get_trash_dir()
    trash_dir.mkdir(exist_ok=True)
    return trash_dir
