"""What a worker declares about the files its engine writes.

An engine drops files in an output directory and the harness has to turn some
of them into response keys. Which files those are, what they are called, and
what an absent one should look like are the engine's business — so they arrive
as data rather than as branches in the packaging code:

    MANIFEST = (
        Artifact("markdown", ("{basename}.md",), kind="text"),
        Artifact(
            "content_list",
            ("{basename}_content_list.json", "{basename}_content_list_v2.json"),
            kind="json",
            default=[],
        ),
        Artifact("images", ("images/*",), kind="b64map"),
    )

Patterns are globs relative to the output directory and are formatted with the
job's ``basename``, which is escaped before substitution — a basename is data,
so a document called ``report[2024]`` resolves to the file of that name rather
than to a character class. Wildcards the engine writes into the pattern itself
still work.

For ``text`` and ``json`` the first pattern that matches wins, which is how an
engine expresses "the new name, or the old one if that is what this version
wrote". Matching more than one file for those kinds is an error rather than a
silent truncation. For ``b64map`` every match across every pattern is
collected, keyed by filename, and a name that appears twice is an error too.

An artifact whose patterns match nothing yields its ``default`` rather than
being dropped, so a caller reading ``response["results"][0]["markdown"]`` gets
an empty string instead of a KeyError when a page produced no text. Each call
gets its own copy of that default: a response is a mutable thing handed to a
caller, and a worker process serves many jobs.

A file that is present but cannot be read yields that same default, and says
so: the substitution is recorded in a
:class:`runpod_doc_worker.contract.degraded.Report` as well as logged, because
an empty value on its own cannot be told apart from a page that had no text.
An engine that cannot produce a useful response without a particular artifact
declares it ``required``, and an absent or unreadable one raises
:class:`ArtifactError` instead.
"""

from __future__ import annotations

import base64
import copy
import glob as _glob
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from runpod_doc_worker import paths as _paths
from runpod_doc_worker.contract import degraded as _degraded


TEXT = "text"
JSON = "json"
B64MAP = "b64map"

VALID_KINDS = (TEXT, JSON, B64MAP)

# Kinds that name one file. Everything else collects.
_SINGLE_VALUE_KINDS = (TEXT, JSON)

# Distinguishes "no default given, derive one from the kind" from a default of
# None, which an engine is allowed to want.
_UNSET = object()

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




# Factories, not values — see the module docstring on why a shared container
# would be a bug rather than an optimisation.
_DERIVED_DEFAULTS: dict[str, Any] = {
    TEXT: str,
    JSON: dict,
    B64MAP: dict,
}


class ArtifactError(RuntimeError):
    """An engine's output could not be turned into a response.

    Separate from the ``ValueError``s this module raises, which mean a manifest
    is declared wrong — a programmer error, the same on every job until someone
    fixes it. This one is a condition of one output directory: a file the
    manifest says the response cannot do without is absent or unreadable on
    this job and may well be fine on the next. A worker that wants to tell a
    caller's bad input apart from its own engine's bad output catches this.
    """


@dataclass(frozen=True)
class Artifact:
    """One response key, and the file(s) behind it.

    :param key: Name this appears under in the response entry.
    :param patterns: Globs relative to the output directory, formatted with
        ``basename``. Order matters for ``text`` and ``json``.
    :param kind: ``text``, ``json`` or ``b64map``.
    :param default: Value when nothing matches. Derived from ``kind`` when not
        given: ``""`` for text, ``{}`` for json and b64map. Copied per read.
    :param required: Whether a response is worth returning without this. The
        default, False, is the harness's usual trade: substitute, report it
        under ``degraded``, ship the rest. Set it for the artifact that *is*
        the job — the one whose empty value makes the whole response
        pointless — and an absent or unreadable file raises
        :class:`ArtifactError` instead. Single-value kinds only; see
        ``__post_init__`` for why a collection cannot express it.
    """

    key: str
    patterns: tuple[str, ...]
    kind: str = TEXT
    default: Any = _UNSET
    required: bool = False

    def __post_init__(self) -> None:
        if not self.key or not isinstance(self.key, str):
            raise ValueError(f"artifact key must be a non-empty string; got {self.key!r}")
        if self.kind not in VALID_KINDS:
            raise ValueError(
                f"artifact {self.key!r}: kind must be one of {list(VALID_KINDS)}; "
                f"got {self.kind!r}"
            )
        if isinstance(self.patterns, str):
            # A bare string is iterable, so this would otherwise be accepted and
            # then fail deep in packaging with a format-string error.
            raise ValueError(
                f"artifact {self.key!r}: patterns must be a tuple of strings, "
                f"not a bare string — did you mean ({self.patterns!r},)?"
            )
        if not self.patterns:
            raise ValueError(f"artifact {self.key!r}: at least one pattern is required")
        for pattern in self.patterns:
            if not isinstance(pattern, str):
                raise ValueError(
                    f"artifact {self.key!r}: patterns must be strings; "
                    f"got {type(pattern).__name__}"
                )
        if self.required and self.kind == B64MAP:
            # A collection has no single file to require. "At least one member"
            # and "every member readable" are different assertions, and neither
            # is what `required` says elsewhere — so rather than pick one and
            # surprise whoever assumed the other, refuse it. A per-member
            # failure stays a degradation.
            raise ValueError(
                f"artifact {self.key!r}: required is for {TEXT!r} and {JSON!r} "
                f"artifacts, which name one file. A {B64MAP!r} collects, so an "
                f"unreadable member is reported rather than fatal."
            )
        if self.required and self.default is not _UNSET:
            # One of the two is dead code, and which one is not obvious from
            # the declaration: a required artifact never falls back, so the
            # default can never be read.
            raise ValueError(
                f"artifact {self.key!r}: a required artifact raises rather than "
                f"falling back, so its default would never be used. Drop one."
            )

    @property
    def missing_value(self) -> Any:
        """A fresh copy of this artifact's default, safe for a caller to mutate."""
        if self.default is _UNSET:
            return _DERIVED_DEFAULTS[self.kind]()
        return copy.deepcopy(self.default)

    def matches(
        self,
        output_dir: Path,
        basename: str,
        report: _degraded.Report | None = None,
        *,
        record_unresolvable: bool = True,
        unresolvable: set[Path] | None = None,
    ) -> list[Path]:
        """Files this artifact resolves to, in pattern order then name order."""
        check_basename(basename)
        report = _degraded.sink(report)
        found: list[Path] = []
        reported_unresolvable: set[Path] = set()
        for pattern in self.patterns:
            expanded = pattern.format(basename=_glob.escape(basename))
            hits: list[Path] = []
            for p in _glob_hits(output_dir, expanded):
                # A dangling link can resolve lexically outside the tree when
                # its missing target is outside it. Its kind is still unknown,
                # so report that fact rather than treating it as evidence of an
                # escape. Existing outside files and directories reach the
                # containment check below.
                what = _paths.kind(p)
                if what == _paths.UNRESOLVABLE:
                    if p not in reported_unresolvable:
                        reported_unresolvable.add(p)
                        if unresolvable is not None:
                            unresolvable.add(p)
                        if record_unresolvable:
                            report.note(
                                reason=_degraded.UNRESOLVABLE,
                                file=p.name,
                                artifact=self.key,
                            )
                    continue
                where = _paths.relation(output_dir, p)
                if where == _paths.OUTSIDE:
                    raise ValueError(
                        f"artifact {self.key!r}: pattern {pattern!r} matched "
                        f"{p}, which is outside the output directory. An "
                        f"engine reads its own output, not whatever sits next to it."
                    )
                if where == _paths.UNRESOLVABLE:
                    # Not an escape: the filesystem would not say where this is.
                    # Unusable either way, so it is dropped like an unreadable
                    # file rather than failing a job over a traversal that
                    # nothing has evidence of.
                    if p not in reported_unresolvable:
                        reported_unresolvable.add(p)
                        if unresolvable is not None:
                            unresolvable.add(p)
                        if record_unresolvable:
                            report.note(
                                reason=_degraded.UNRESOLVABLE,
                                file=p.name,
                                artifact=self.key,
                            )
                    continue
                # Skipped only after containment: an outside directory link is
                # rejected above, while an ordinary in-tree directory remains
                # a silent non-match.
                if what == _paths.DIRECTORY:
                    continue
                hits.append(p)
            if not hits:
                continue
            if self.kind in _SINGLE_VALUE_KINDS:
                if len(hits) > 1:
                    raise ValueError(
                        f"artifact {self.key!r} is a single-value {self.kind!r} "
                        f"artifact but pattern {pattern!r} matched "
                        f"{len(hits)} files: "
                        f"{', '.join(p.name for p in hits)}. Narrow the pattern, "
                        f"or declare it as {B64MAP!r} to collect them all."
                    )
                # First pattern that matched decides it — later patterns are
                # fallbacks, not additions.
                return hits
            found.extend(hits)

        if self.kind == B64MAP:
            counts = Counter(p.name for p in found)
            collisions = sorted(n for n, c in counts.items() if c > 1)
            if collisions:
                raise ValueError(
                    f"artifact {self.key!r} collected more than one file named "
                    f"{', '.join(repr(n) for n in collisions)}. Keys are "
                    f"filenames, so these would overwrite each other — narrow "
                    f"the patterns or split them into separate artifacts."
                )
        return found

    def read(
        self,
        output_dir: Path,
        basename: str,
        report: _degraded.Report | None = None,
    ) -> Any:
        """Value for this artifact, or a fresh default when nothing matched."""
        report = _degraded.sink(report)
        hits = self.matches(output_dir, basename, report)
        if not hits:
            if self.required:
                raise ArtifactError(
                    f"artifact {self.key!r} is required and matched no file. "
                    f"Patterns tried, against basename {basename!r}: "
                    f"{', '.join(repr(p) for p in self.patterns)}."
                )
            return self.missing_value

        if self.kind == TEXT:
            try:
                return hits[0].read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as e:
                return self._unreadable(hits[0], e, report)

        if self.kind == JSON:
            try:
                return json.loads(hits[0].read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
                return self._unreadable(hits[0], e, report)

        # A collection falls back per member rather than wholesale: a file that
        # vanished between matching and reading should cost the response that
        # one entry, not the other forty and the job with them.
        collected: dict[str, str] = {}
        for p in hits:
            try:
                collected[p.name] = base64.b64encode(p.read_bytes()).decode("ascii")
            except OSError as e:
                self._note_unreadable(p, e, report)
        return collected

    def validate_buffered(
        self,
        path: Path,
        data: bytes,
        report: _degraded.Report | None = None,
    ) -> None:
        """Validate one single-value artifact from already captured bytes.

        Archive packaging uses this to validate the exact bytes it will ship,
        rather than validating a file and then reading it again later.
        """
        if self.kind not in _SINGLE_VALUE_KINDS:
            raise ValueError("buffered reads require a single-value artifact")
        report = _degraded.sink(report)
        try:
            text = data.decode("utf-8")
            if self.kind == JSON:
                json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._unreadable(path, exc, report)

    def _note_unreadable(
        self, path: Path, exc: Exception, report: _degraded.Report
    ) -> None:
        """Record that a file could not be read, and log it.

        A truncated or unreadable artifact is the engine's problem to fix, and
        it must not take down a response that is otherwise complete. But a
        response that silently substitutes an empty value is a defect nobody
        can count — so the substitution goes into the response as well as the
        log, because at scale the log line is not what anyone reads.
        """
        report.note(
            reason=_degraded.UNREADABLE,
            file=path.name,
            artifact=self.key,
            error_type=type(exc).__name__,
        )

    def _unreadable(
        self, path: Path, exc: Exception, report: _degraded.Report
    ) -> Any:
        """Note, then fall back to this artifact's default — or raise.

        The note happens either way. A required artifact fails the job, and the
        log line saying which file and which error is the same one an operator
        needs to work out why, so it is not worth skipping on the path where
        the response will not survive to carry it.
        """
        self._note_unreadable(path, exc, report)
        if self.required:
            raise ArtifactError(
                f"artifact {self.key!r} is required and {path.name} could not be "
                f"read: {type(exc).__name__}."
            ) from exc
        return self.missing_value


def validate(manifest: Iterable[Artifact]) -> tuple[Artifact, ...]:
    """Check a manifest as a whole. Returns it as a tuple.

    Per-artifact rules are enforced at construction; this catches the one that
    only exists between artifacts — two entries claiming the same response key,
    where ``keys()`` would advertise both and ``resolve()`` would return one.
    """
    entries = tuple(manifest)
    counts = Counter(a.key for a in entries)
    duplicates = sorted(k for k, c in counts.items() if c > 1)
    if duplicates:
        raise ValueError(
            f"manifest declares duplicate keys: {', '.join(duplicates)}. "
            f"Each response key may come from exactly one artifact."
        )
    return entries


def resolve(
    manifest: Iterable[Artifact],
    output_dir: Path,
    basename: str,
    keys: Iterable[str] | None = None,
    report: _degraded.Report | None = None,
) -> dict[str, Any]:
    """Read a manifest into a response dict.

    ``keys`` filters which artifacts are read at all — a filtered-out artifact
    is omitted from the result, not present-as-empty, so a caller asking for
    markdown only does not pay to base64 every image.

    ``report`` collects anything that had to be dropped or substituted on the
    way. Pass one when the result is going to a caller: without it the drops
    are still logged, but the response cannot say a value is a fallback rather
    than a genuinely empty artifact. See
    :mod:`runpod_doc_worker.contract.degraded`.
    """
    entries = validate(manifest)
    wanted = set(keys) if keys is not None else None
    if wanted is not None:
        # A key nobody declared used to be dropped in silence, so a typo came
        # back as a successful response with nothing in it — which reads as
        # "this document produced no output" rather than "you asked for
        # something that does not exist". A worker's own schema usually catches
        # this first; a caller composing these functions directly has nothing
        # else to catch it.
        unknown = wanted - {art.key for art in entries}
        if unknown:
            raise ValueError(
                f"requested format(s) {', '.join(sorted(unknown))} are not in "
                f"the manifest; it declares "
                f"{', '.join(art.key for art in entries)}"
            )
    report = _degraded.sink(report)
    return {
        art.key: art.read(output_dir, basename, report)
        for art in entries
        if wanted is None or art.key in wanted
    }


def keys(manifest: Iterable[Artifact]) -> list[str]:
    """Response keys a manifest can produce, in declaration order."""
    return [art.key for art in validate(manifest)]
