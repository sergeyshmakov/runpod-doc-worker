"""The two distributions this repo builds carry the same version.

There are two: `runpod-doc-worker` at the root and `runpod-doc-client` under
`client/`. The release rewrote only the first, so v0.7.0 shipped client code that
declared itself 0.6.0.

That is not cosmetic. The client is installed from a tag tarball, so pip saw the
version it already had and skipped the upgrade — a consumer who moved their pin
from v0.6.0 to v0.7.0 got none of the new code and no warning. A release that does
not arrive is worse than one that fails to build, because nothing says so.

The distributions are **discovered** here rather than listed. A hard-coded list
would have made every test below pass for a third distribution nobody had wired
into the release — which is the regression these exist to catch, reproduced one
level up. The release script keeps an explicit list, because a release step that
rewrites whatever it finds is the wrong kind of clever; the test is what makes the
two agree.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Not ours to version.
SKIP = {".venv", "node_modules", "__pycache__", ".git", ".wolf", "out", "dist", "build"}

_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_NAME = re.compile(r'^name\s*=\s*"([^"]+)"', re.MULTILINE)


def _discover() -> dict[str, str]:
    """Every pyproject in the tree that declares a distribution and a version."""
    found: dict[str, str] = {}
    for path in REPO.rglob("pyproject.toml"):
        relative = path.relative_to(REPO)
        if any(part in SKIP for part in relative.parts):
            continue
        text = path.read_text(encoding="utf-8")
        version, name = _VERSION.search(text), _NAME.search(text)
        if version and name:
            found[relative.as_posix()] = version.group(1)
    return found


def _release_config() -> dict:
    return json.loads((REPO / ".releaserc.json").read_text(encoding="utf-8"))


def _git_assets() -> list[str]:
    """The `assets` array of the @semantic-release/git plugin, parsed.

    Parsed rather than searched for as text. `"pyproject.toml" in raw_json` is true
    whenever `client/pyproject.toml` is listed, so a substring check passed even
    with the root file removed from the release commit — which would leave
    semantic-release rewriting a version it never committed.
    """
    for plugin in _release_config()["plugins"]:
        if isinstance(plugin, list) and plugin[0].endswith("/git"):
            return list(plugin[1].get("assets", []))
    raise AssertionError("no @semantic-release/git plugin in .releaserc.json")


def _script_targets() -> list[str]:
    """The list the release script actually rewrites, read from the script."""
    path = REPO / "scripts" / "set_version.py"
    spec = importlib.util.spec_from_file_location("_set_version", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [Path(target).as_posix() for target in module.TARGETS]


def test_the_discovery_finds_both_known_distributions() -> None:
    """The guard on every test below: discovery that silently found nothing would
    make all of them vacuous."""
    discovered = _discover()
    assert "pyproject.toml" in discovered
    assert "client/pyproject.toml" in discovered


def test_every_distribution_declares_the_same_version() -> None:
    versions = _discover()
    assert len(set(versions.values())) == 1, (
        f"the distributions have drifted apart: {versions}. The release writes "
        f"them through scripts/set_version.py; if one is stale, that step did not "
        f"run or the file is not listed there."
    )


def test_the_release_script_rewrites_every_distribution() -> None:
    """Discovered against listed. A third distribution added later fails here until
    someone adds it to the script, which is the point -- the previous version of
    this test compared the script against a copy of its own list."""
    assert set(_script_targets()) == set(_discover()), (
        f"scripts/set_version.py rewrites {sorted(_script_targets())} but the tree "
        f"has {sorted(_discover())}"
    )


def test_the_release_commits_every_distribution() -> None:
    """Rewriting a file the release does not commit leaves the tag carrying the old
    version, so the drift returns on the next release."""
    assets = _git_assets()
    for name in _discover():
        assert name in assets, (
            f"{name} is not in the @semantic-release/git assets {assets}; its "
            f"version would be rewritten and then left out of the release commit"
        )


def test_the_release_calls_the_script() -> None:
    exec_plugins = [
        plugin[1]
        for plugin in _release_config()["plugins"]
        if isinstance(plugin, list) and plugin[0].endswith("/exec")
    ]
    assert any(
        "scripts/set_version.py" in plugin.get("prepareCmd", "")
        for plugin in exec_plugins
    ), "the release no longer calls the script that writes the versions"


@pytest.mark.parametrize("name", ["pyproject.toml", "client/pyproject.toml"])
def test_the_script_writes_a_version_it_is_given(name: str, tmp_path) -> None:
    """Run for real, in a copy, because a regex that silently matches nothing is
    exactly how the original failure would have looked in review."""
    for target in _discover():
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
