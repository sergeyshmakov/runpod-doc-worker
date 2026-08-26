"""Reading a directory without trusting it to be small, or to be a directory.

Both probes need this and neither owns it. Each call is bounded, and each answer
about a directory entry is taken without following a link -- a symlinked cache
entry that points at a loop would otherwise turn a bounded walk into an unbounded
one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _scan(directory: Path, max_entries: int) -> tuple[list[Any], bool]:
    """Up to ``max_entries`` raw directory entries. Returns ``(entries, more)``.

    Uses ``os.scandir`` rather than ``Path.iterdir`` because it is the only one
    that is lazy on every interpreter this package supports: ``iterdir`` is a
    generator through 3.12 and materialises the whole scandir result from 3.13,
    so slicing it bounds the response and not the work.

    The cap counts entries **read**, before any filtering. Filtering first and
    slicing after counts only what survives the filter, so a directory holding
    ten thousand files and three subdirectories is read in full to prove there
    is no fourth subdirectory.
    """
    entries: list[Any] = []
    more = False
    with os.scandir(directory) as scan:
        for entry in scan:
            if len(entries) >= max_entries:
                more = True
                break
            entries.append(entry)
    return entries, more


def _is_dir(entry: Any) -> bool:
    """``entry.is_dir()`` without letting a stat failure escape."""
    try:
        return entry.is_dir()
    except OSError:
        return False


def _is_dir_nofollow(entry: Any) -> bool:
    """``entry.is_dir()`` that refuses to be led out of the tree by a symlink."""
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False
