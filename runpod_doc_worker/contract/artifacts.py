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
job's ``basename``. For ``text`` and ``json`` the first pattern that matches a
file wins, which is how an engine expresses "the new name, or the old one if
that is what this version wrote". For ``b64map`` every match across every
pattern is collected, keyed by filename.

An artifact whose patterns match nothing yields its ``default`` rather than
being dropped, so a caller reading ``response["results"][0]["markdown"]`` gets
an empty string instead of a KeyError when a page produced no text.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


TEXT = "text"
JSON = "json"
B64MAP = "b64map"

VALID_KINDS = (TEXT, JSON, B64MAP)

# Distinguishes "no default given, derive one from the kind" from a default of
# None, which an engine is allowed to want.
_UNSET = object()

_DERIVED_DEFAULTS: dict[str, Any] = {
    TEXT: "",
    JSON: {},
    B64MAP: {},
}


@dataclass(frozen=True)
class Artifact:
    """One response key, and the file(s) behind it.

    :param key: Name this appears under in the response entry.
    :param patterns: Globs relative to the output directory, formatted with
        ``basename``. Order matters for ``text`` and ``json``.
    :param kind: ``text``, ``json`` or ``b64map``.
    :param default: Value when nothing matches. Derived from ``kind`` when not
        given: ``""`` for text, ``{}`` for json and b64map.
    """

    key: str
    patterns: tuple[str, ...]
    kind: str = TEXT
    default: Any = _UNSET

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise ValueError(
                f"artifact {self.key!r}: kind must be one of {list(VALID_KINDS)}; "
                f"got {self.kind!r}"
            )
        if not self.patterns:
            raise ValueError(f"artifact {self.key!r}: at least one pattern is required")

    @property
    def missing_value(self) -> Any:
        if self.default is _UNSET:
            return _DERIVED_DEFAULTS[self.kind]
        return self.default

    def matches(self, output_dir: Path, basename: str) -> list[Path]:
        """Files this artifact resolves to, in pattern order then name order."""
        found: list[Path] = []
        for pattern in self.patterns:
            hits = sorted(
                p for p in output_dir.glob(pattern.format(basename=basename))
                if p.is_file()
            )
            if not hits:
                continue
            if self.kind == B64MAP:
                found.extend(hits)
            else:
                # First pattern that matched decides it — later patterns are
                # fallbacks, not additions.
                return hits[:1]
        return found

    def read(self, output_dir: Path, basename: str) -> Any:
        """Value for this artifact, or its default when nothing matched."""
        hits = self.matches(output_dir, basename)
        if not hits:
            return self.missing_value

        if self.kind == TEXT:
            return hits[0].read_text(encoding="utf-8")

        if self.kind == JSON:
            try:
                return json.loads(hits[0].read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # A truncated or non-JSON file is the engine's problem to fix,
                # but it must not take down a response that is otherwise fine.
                return self.missing_value

        return {
            p.name: base64.b64encode(p.read_bytes()).decode("ascii")
            for p in hits
        }


def resolve(
    manifest: Iterable[Artifact],
    output_dir: Path,
    basename: str,
    keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Read a manifest into a response dict.

    ``keys`` filters which artifacts are read at all — a filtered-out artifact
    is omitted from the result, not present-as-empty, so a caller asking for
    markdown only does not pay to base64 every image.
    """
    wanted = set(keys) if keys is not None else None
    return {
        art.key: art.read(output_dir, basename)
        for art in manifest
        if wanted is None or art.key in wanted
    }


def keys(manifest: Iterable[Artifact]) -> list[str]:
    """Response keys a manifest can produce, in declaration order."""
    return [art.key for art in manifest]
