"""One answer to "does this path stay inside the tree I was given".

Three modules needed this question answered and each grew its own version:
packaging asked it of archive members, the artifact manifest asked it of glob
matches, and the probe asked it of cache entries. Review found the same class
of escape in each, at different times, because a check living in three places
is a check that gets fixed in one of them.

The question is always the same and the answer should be too: resolve both
sides, then compare. Resolving is the point — a symlink, a ``..`` segment or a
non-canonical spelling all look contained until they are resolved, and every
instance of this bug in this package has been something that looked contained.
"""

from __future__ import annotations

from pathlib import Path


def within(root: Path, candidate: Path) -> bool:
    """Whether ``candidate`` really sits at or beneath ``root`` once resolved.

    Returns False when either side cannot be resolved. A path that cannot be
    examined is not one to trust: the callers of this either skip such an entry
    or report it, and both are better than assuming it is fine.
    """
    try:
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve()
    except OSError:
        return False
    return (
        candidate_resolved == root_resolved
        or root_resolved in candidate_resolved.parents
    )


def escapes(root: Path, candidate: Path) -> bool:
    """The negation of :func:`within`, for call sites that read better that way."""
    return not within(root, candidate)
