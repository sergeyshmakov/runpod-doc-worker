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

There are three answers, not two. A path that resolves outside the root is the
engine having laid out its directory in a way a caller must not be served; a
path that cannot be resolved at all is the filesystem declining to answer — a
symlink loop, a permission error on a parent directory. Collapsing the second
into the first is how a permission error comes back reported as an escape,
which sends whoever reads it hunting a traversal that was never there.
"""

from __future__ import annotations

from pathlib import Path


INSIDE = "inside"
OUTSIDE = "outside"
UNRESOLVABLE = "unresolvable"


def relation(root: Path, candidate: Path) -> str:
    """Where ``candidate`` sits relative to ``root``: inside, outside, or unknown.

    Returns one of :data:`INSIDE`, :data:`OUTSIDE`, :data:`UNRESOLVABLE`. Ask
    this rather than :func:`within` when the two failing answers need different
    handling — reporting the right cause, or treating a file the filesystem
    will not describe as unusable rather than as an escape attempt.
    """
    try:
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return UNRESOLVABLE
    if (
        candidate_resolved == root_resolved
        or root_resolved in candidate_resolved.parents
    ):
        return INSIDE
    return OUTSIDE


def within(root: Path, candidate: Path) -> bool:
    """Whether ``candidate`` really sits at or beneath ``root`` once resolved.

    Returns False when either side cannot be resolved. A path that cannot be
    examined is not one to trust: the callers of this either skip such an entry
    or report it, and both are better than assuming it is fine.
    """
    return relation(root, candidate) == INSIDE


def escapes(root: Path, candidate: Path) -> bool:
    """The negation of :func:`within`, for call sites that read better that way."""
    return not within(root, candidate)
