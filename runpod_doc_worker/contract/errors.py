"""The error type the artifact contract raises.

Its own module so that both the manifest and the path helpers can raise it
without either importing the other.
"""

from __future__ import annotations


class ArtifactError(RuntimeError):
    """An engine's output could not be turned into a response.

    Separate from the ``ValueError``s this module raises, which mean a manifest
    is declared wrong — a programmer error, the same on every job until someone
    fixes it. This one is a condition of one output directory: a file the
    manifest says the response cannot do without is absent or unreadable on
    this job and may well be fine on the next. A worker that wants to tell a
    caller's bad input apart from its own engine's bad output catches this.
    """
