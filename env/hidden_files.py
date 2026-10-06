"""Shared predicates for system files hidden from the browser (#81).

Two layers, deliberately kept separate:

- ``is_system_junk_name`` — macOS AppleDouble sidecars (``._foo.jpg``) or an
  exact match (case-insensitive) against BROWSER_HIDDEN_NAMES (``.DS_Store``,
  ``Thumbs.db``, ``desktop.ini``, Insta360's ``fileinfo_list.list`` is NOT
  here — it's matched by extension, see below). This is the narrower check
  used to decide whether a directory counts as *empty*: these names are pure
  OS/tool junk, never real user or companion data.
- ``is_hidden_system_file`` — the full "hide this from the browser" rule:
  everything ``is_system_junk_name`` covers, plus any extension in
  BROWSER_HIDDEN_EXTENSIONS. Those extensions are sidecar files (``.xmp``,
  ``.list``, ...) that travel with a tracked file on move/rename
  (fileops/service.py's ``_sidecars_for``) and carry real companion data —
  they must stay out of the "is this folder empty" decision, but they are
  still hidden from directory listings/counts/tracking like any other
  system file.
"""

from pathlib import Path


def is_system_junk_name(name: str, hidden_names: set[str]) -> bool:
    """True for a macOS AppleDouble sidecar or a name in BROWSER_HIDDEN_NAMES
    (case-insensitive). Never true for a BROWSER_HIDDEN_EXTENSIONS sidecar —
    use ``is_hidden_system_file`` for that."""
    if name.startswith('._'):
        return True
    return name.lower() in hidden_names


def is_hidden_system_file(name: str, hidden_extensions: set[str], hidden_names: set[str]) -> bool:
    """Full "hide from the browser" predicate: junk names (see
    ``is_system_junk_name``) plus any BROWSER_HIDDEN_EXTENSIONS sidecar."""
    if is_system_junk_name(name, hidden_names):
        return True
    return Path(name).suffix.lower() in hidden_extensions
