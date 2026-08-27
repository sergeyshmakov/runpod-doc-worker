"""The package must not know which engine it is serving.

This is the failure mode the whole extraction exists to prevent: one
`if engine == ...`, one hard-coded model glob, one env var named after a
particular worker, and the harness quietly becomes a fork of that worker again.

Names are checked in source rather than behaviour because that is where the
leak shows up first — usually in a docstring or an error message that was
copied across without being reread.

What this cannot catch: an engine's *vocabulary* that shares no substring with
its name. A backend identifier, a model nickname, a flag spelling — all read as
ordinary words. A review caught exactly that (a worker's production backend
value sitting in a log fixture) after this file was already green. So the list
below is a floor, not a proof: when adding a fixture value, ask whether it
would mean anything to someone who had never heard of the engine, and if not,
invent one that would.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import runpod_doc_worker


PACKAGE_ROOT = Path(runpod_doc_worker.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

# Engines this harness is expected to serve, the orgs that ship them, and the
# runtimes they happen to be served on. The runtimes are here because they are
# how the leak usually arrives: a docstring explaining what a field is for, in
# terms of the one engine the author had in mind.
FORBIDDEN = (
    "mineru",
    "opendatalab",
    "paddle",
    "vllm",
    "sglang",
)

# Prose about the extraction itself has to name the repos it moves code between,
# and this file has to spell the words it bans.
EXEMPT = (
    "plans",
    "CHANGELOG.md",
    "test_no_engine_names.py",
)


def _checked_files() -> list[Path]:
    """Package sources, tests, and the docs that ship with them."""
    found: list[Path] = []
    for pattern in (
        "runpod_doc_worker/**/*.py",
        # The client half is engine-neutral for the same reason and was not
        # scanned, so a module naming both engines passed this gate.
        "client/**/*.py",
        "tests/**/*.py",
        "*.md",
    ):
        found.extend(REPO_ROOT.glob(pattern))
    return sorted(
        p for p in found
        if not any(part in EXEMPT for part in p.relative_to(REPO_ROOT).parts)
    )


def test_there_are_files_to_check():
    """Guard against the globs silently matching nothing and passing forever."""
    checked = _checked_files()
    assert len(checked) >= 15, f"only {len(checked)} files matched"
    names = {p.name for p in checked}
    assert {"io.py", "package.py", "README.md", "CONTRIBUTING.md"} <= names


@pytest.mark.parametrize("name", FORBIDDEN)
def test_no_engine_name_appears_anywhere(name):
    pattern = re.compile(name, re.IGNORECASE)
    offenders: list[str] = []
    for path in _checked_files():
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                rel = path.relative_to(REPO_ROOT).as_posix()
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        f"{name!r} appears in the harness. Engine-specific names belong in the "
        f"worker repo — pass the value through WorkerConfig or an artifact "
        f"manifest instead, and describe fields in terms of what any engine "
        f"would want:\n  " + "\n  ".join(offenders)
    )
