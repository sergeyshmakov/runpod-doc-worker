"""Turning a caller's basename and a manifest pattern into real paths.

A different question from the manifest's own: not "what artifacts are declared"
but "which files on disk does this name reach". Both halves have been a source of
surprises -- a basename with a path separator in it, and a glob whose
metacharacters came from the caller -- and neither is about the manifest.
"""

from __future__ import annotations

import glob as _glob
import os
from pathlib import Path

from runpod_doc_worker import paths as _paths

# Anything that could steer a formatted pattern out of the directory it was
# given. Escaping handles glob syntax; separators survive it untouched.
_BASENAME_SEPARATORS = ("/", "\\")


def _glob_hits(output_dir: Path, pattern: str) -> list[Path]:
    """Pathlib matches, plus broken entries older precise selectors omit.

    Python 3.10 and 3.11 implement a literal path component by asking whether
    its target exists, so an exact pattern silently loses a dangling link or a
    symlink loop. Keep ``Path.glob`` as the source of ordinary matches, then
    probe an exact final component beneath the parents it already matched.
    This preserves its ordering, duplicates, dotfile rules and recursive
    symlink behaviour rather than introducing a second glob implementation.
    """
    hits = list(output_dir.glob(pattern))
    parts = Path(pattern).parts
    if not parts or _glob.has_magic(parts[-1]):
        return sorted(hits)

    seen = set(hits)
    parents = (output_dir,)
    if len(parts) > 1:
        parents = output_dir.glob(str(Path(*parts[:-1])))
    for parent in parents:
        candidate = parent / parts[-1]
        if candidate in seen:
            continue
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            # The entry is named but cannot be stated; ``kind`` maps the same
            # error to UNRESOLVABLE so the caller can report it.
            pass
        except ValueError:
            continue
        if _paths.kind(candidate) == _paths.UNRESOLVABLE:
            hits.append(candidate)
            seen.add(candidate)
    return sorted(hits)


def check_basename(basename: str) -> None:
    """Reject a basename that could read outside the output directory.

    A basename is a caller-supplied string in every worker that has one, and it
    is substituted into a pattern that is then globbed. ``glob.escape`` makes
    it literal as far as glob syntax goes, but leaves ``/``, ``\\`` and ``..``
    alone — so ``{basename}.md`` with ``../other/doc`` reads a sibling
    directory, which on a worker serving many jobs is another job's output.

    Workers are expected to constrain this at their own schema too. This is the
    check that does not depend on them having done so.
    """
    if not isinstance(basename, str) or not basename:
        raise ValueError(f"basename must be a non-empty string; got {basename!r}")
    for sep in _BASENAME_SEPARATORS:
        if sep in basename:
            raise ValueError(
                f"basename may not contain a path separator; got {basename!r}"
            )
    # With separators gone, these are the only spellings left that name a
    # directory rather than a file in it.
    if basename in (".", ".."):
        raise ValueError(f"basename may not be a path traversal; got {basename!r}")
