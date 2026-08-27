"""The two distributions this repo builds carry the same version.

There are two: `runpod-doc-worker` at the root and `runpod-doc-client` under
`client/`. The release rewrote only the first, so v0.7.0 shipped client code that
declared itself 0.6.0.

That is not cosmetic. The client is installed from a tag tarball, so pip saw the
version it already had and skipped the upgrade — a consumer who moved their pin
from v0.6.0 to v0.7.0 got none of the new code and no warning. A release that does
not arrive is worse than one that fails to build, because nothing says so.

Checked here rather than trusted to the release config, since the config is what
was wrong.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PYPROJECTS = ("pyproject.toml", "client/pyproject.toml")

_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def _declared(name: str) -> str:
    found = _VERSION.search((REPO / name).read_text(encoding="utf-8"))
    assert found, f"{name} declares no version"
    return found.group(1)


def test_every_distribution_declares_the_same_version() -> None:
    versions = {name: _declared(name) for name in PYPROJECTS}
    assert len(set(versions.values())) == 1, (
        f"the distributions have drifted apart: {versions}. The release writes "
        f"both through scripts/set_version.py; if one is stale, that step did not "
        f"run or a file was added without being listed there."
    )


def test_the_release_script_covers_every_pyproject() -> None:
    """The guard on the guard: a third distribution added later has to be added to
    the script too, and the test above would keep passing while it stayed unwritten
    if the script simply did not know about it."""
    script = (REPO / "scripts" / "set_version.py").read_text(encoding="utf-8")
    for name in PYPROJECTS:
        assert name in script.replace("\\", "/"), (
            f"{name} is not named in scripts/set_version.py"
        )


def test_the_release_config_commits_every_pyproject() -> None:
    """Rewriting the file is half of it. If the release does not commit it, the tag
    carries the old version and the drift comes back on the next release."""
    config = (REPO / ".releaserc.json").read_text(encoding="utf-8")
    assert "scripts/set_version.py" in config, (
        "the release no longer calls the script that writes the versions"
    )
    for name in PYPROJECTS:
        assert name in config, f"{name} is not in the release's git assets"


@pytest.mark.parametrize("name", PYPROJECTS)
def test_the_script_writes_a_version_it_is_given(name: str, tmp_path) -> None:
    """Run for real, in a copy, because a regex that silently matches nothing is
    exactly how the original failure would have looked in review."""
    for target in PYPROJECTS:
        destination = tmp_path / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            (REPO / target).read_text(encoding="utf-8"), encoding="utf-8"
        )
    done = subprocess.run(
        ["python", str(REPO / "scripts" / "set_version.py"), "1.2.3"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    found = _VERSION.search((tmp_path / name).read_text(encoding="utf-8"))
    assert found and found.group(1) == "1.2.3", f"{name} was not rewritten"
