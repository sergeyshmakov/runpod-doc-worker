"""Static checks a worker repo can run against its own ``.runpod/hub.json``.

The one that matters: the Hub backend stores every ``description`` in a
``varchar(191)`` column, and anything at or over that length is rejected on
push with an opaque database error. Catching it in CI costs nothing; finding it
by pushing a listing costs an afternoon.

Usage from a worker repo's test suite::

    from runpod_doc_worker.testing import hub

    def test_hub_json():
        hub.check(Path(__file__).parents[1] / ".runpod" / "hub.json")

:func:`check` raises ``AssertionError`` with a message naming the offending
field, so it reads as a test failure wherever it is called from. :func:`problems`
returns the same findings as a list for callers that would rather decide for
themselves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


# RunPod's column is varchar(191). 189 keeps a one-character margin for future
# edits that nudge a description over.
MAX_DESCRIPTION_LENGTH = 189

# Where long-form guidance should go instead of a description field.
_GUIDANCE = "move long-form guidance into the docs rather than the description field"


def load(path: str | Path) -> dict[str, Any]:
    """Parse a hub.json, asserting it exists and is an object."""
    p = Path(path)
    assert p.is_file(), f"hub.json not found at {p}"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"hub.json must be a JSON object; got {type(data).__name__}"
    return data


def problems(hub: dict[str, Any]) -> list[str]:
    """Every issue found, as human-readable strings. Empty means clean."""
    found: list[str] = []

    desc = hub.get("description", "")
    if len(desc) > MAX_DESCRIPTION_LENGTH:
        found.append(
            f"top-level description is {len(desc)} chars "
            f"(max {MAX_DESCRIPTION_LENGTH}); {_GUIDANCE}"
        )

    env_entries = hub.get("config", {}).get("env", [])
    if not env_entries:
        found.append("hub.json has no config.env entries — schema regression?")

    for entry in env_entries:
        key = entry.get("key", "<unknown>")
        if "key" not in entry:
            found.append(f"env entry missing 'key': {entry}")
        inp = entry.get("input", {})
        if "name" not in inp:
            found.append(f"env {key!r} missing input.name")
        if "description" not in inp:
            found.append(f"env {key!r} missing input.description")
        elif not inp["description"]:
            found.append(f"env {key!r} has an empty description")
        elif len(inp["description"]) > MAX_DESCRIPTION_LENGTH:
            found.append(
                f"env {key!r} description is {len(inp['description'])} chars "
                f"(max {MAX_DESCRIPTION_LENGTH}); {_GUIDANCE}"
            )

    return found


def check(path: str | Path) -> None:
    """Assert a hub.json at ``path`` is publishable. Raises AssertionError."""
    found = problems(load(path))
    assert not found, "hub.json problems:\n  - " + "\n  - ".join(found)


def check_test_inputs(path: str | Path, roots: Iterable[str] | None = None) -> None:
    """Assert every ``volume_path`` in a ``.runpod/tests.json`` is reachable.

    The Hub validator runs these jobs against a release build, and a
    ``volume_path`` outside the worker's input roots is rejected there rather
    than here — an expensive place to find out. ``roots`` defaults to the
    harness defaults; pass the worker's own when it narrows them.

    Compared as POSIX paths regardless of the machine running the test: these
    are container paths, not local ones.
    """
    from pathlib import PurePosixPath

    from runpod_doc_worker.transport.io import DEFAULT_VOLUME_ROOTS

    p = Path(path)
    assert p.is_file(), f"tests.json not found at {p}"
    spec = json.loads(p.read_text(encoding="utf-8"))
    allowed = [PurePosixPath(r) for r in (roots if roots is not None else DEFAULT_VOLUME_ROOTS)]

    for case in spec.get("tests", []):
        volume_path = case.get("input", {}).get("volume_path")
        if not volume_path:
            continue
        target = PurePosixPath(volume_path)
        assert any(r == target or r in target.parents for r in allowed), (
            f"{volume_path!r} from {p.name} is outside the input roots "
            f"({', '.join(str(r) for r in allowed)}) — the Hub validator would "
            f"reject it"
        )
