"""Write the release version into every distribution this repo builds.

Called by semantic-release's `prepare` step. There are two distributions here --
`runpod-doc-worker` at the root and `runpod-doc-client` under `client/` -- and the
release used to rewrite only the first.

The consequence was worse than a cosmetic mismatch. The client distribution is
installed from a tag tarball, so v0.7.0 shipped code that declared itself 0.6.0;
pip then saw the version it already had and skipped the upgrade. A consumer who
moved their pin from v0.6.0 to v0.7.0 got no new code and no warning, which is the
quietest possible way for a release not to arrive.

Kept as a script rather than an inline `python -c` in .releaserc.json, where the
JSON escaping made it unreadable and adding a second file to it error-prone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Every pyproject that carries a version this release owns.
TARGETS = (
    Path("pyproject.toml"),
    Path("client/pyproject.toml"),
)

_VERSION = re.compile(r'^version\s*=\s*"[^"]+"', re.MULTILINE)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <version>", file=sys.stderr)
        return 2
    version = argv[1]
    for target in TARGETS:
        if not target.is_file():
            print(f"missing {target}", file=sys.stderr)
            return 1
        text = target.read_text(encoding="utf-8")
        updated, count = _VERSION.subn(f'version = "{version}"', text, count=1)
        if count != 1:
            print(f"no version field in {target}", file=sys.stderr)
            return 1
        target.write_text(updated, encoding="utf-8")
        print(f"{target}: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
