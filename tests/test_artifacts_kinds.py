"""The artifact manifest: what a worker's output directory turns into. -- kinds."""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

import pytest

from runpod_doc_worker.contract.artifacts import Artifact, resolve

# `*` and `?` are legal in POSIX filenames and illegal in Windows ones, so the
# cases that need such a file on disk only run where one can exist. The escaping
# they cover is platform-independent.
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="filename is not creatable on Windows"
)


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


@pytest.fixture
def output_dir(tmp_path):
    (tmp_path / "doc.md").write_text("# hello\n", encoding="utf-8")
    (tmp_path / "doc_content_list.json").write_text('[{"type": "text"}]', encoding="utf-8")
    images = tmp_path / "images"
    images.mkdir()
    (images / "fig1.png").write_bytes(b"\x89PNG\r\n\x1a\nfig1")
    (images / "fig2.png").write_bytes(b"\x89PNG\r\n\x1a\nfig2")
    return tmp_path


def test_b64map_values_round_trip(output_dir):
    out = resolve(MANIFEST, output_dir, "doc")
    assert base64.b64decode(out["images"]["fig1.png"]) == b"\x89PNG\r\n\x1a\nfig1"


def test_derived_defaults_when_none_declared(tmp_path):
    manifest = (
        Artifact("text_thing", ("nope.txt",), kind="text"),
        Artifact("json_thing", ("nope.json",), kind="json"),
        Artifact("map_thing", ("nope/*",), kind="b64map"),
    )
    out = resolve(manifest, tmp_path, "doc")
    assert out == {"text_thing": "", "json_thing": {}, "map_thing": {}}


def test_b64map_name_collision_across_patterns_raises(tmp_path):
    """Keys are filenames, so two dirs with the same name would overwrite."""
    manifest = (Artifact("images", ("images/*", "figures/*"), kind="b64map"),)
    for sub in ("images", "figures"):
        d = tmp_path / sub
        d.mkdir()
        (d / "fig1.png").write_bytes(sub.encode())
    with pytest.raises(ValueError, match="more than one file named"):
        resolve(manifest, tmp_path, "doc")


def test_b64map_across_patterns_without_collision_is_fine(tmp_path):
    manifest = (Artifact("images", ("images/*", "figures/*"), kind="b64map"),)
    for sub, name in (("images", "a.png"), ("figures", "b.png")):
        d = tmp_path / sub
        d.mkdir()
        (d / name).write_bytes(b"x")
    assert sorted(resolve(manifest, tmp_path, "doc")["images"]) == ["a.png", "b.png"]


def test_an_unreadable_b64map_member_is_skipped(tmp_path, capsys, monkeypatch):
    """A file can vanish between matching and reading. Text and json fall back
    with a warning; a collection must not abort the whole response instead."""
    images = tmp_path / "images"
    images.mkdir()
    (images / "fig1.png").write_bytes(b"one")
    (images / "fig2.png").write_bytes(b"two")

    real_read_bytes = Path.read_bytes

    def flaky(self):
        if self.name == "fig1.png":
            raise OSError(2, "No such file or directory")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    out = resolve(MANIFEST, tmp_path, "doc")
    assert list(out["images"]) == ["fig2.png"]

    warning = json.loads(capsys.readouterr().out.strip())
    assert warning["level"] == "warning"
    assert warning["file"] == "fig1.png"


@pytest.mark.parametrize(
    "kind,data",
    [("text", b"# valid text\n"), ("json", b'{"valid": true}')],
)
def test_stream_validation_only_requires_python_310_spool_methods(
    tmp_path, kind, data
):
    class Python310Spool:
        """The 3.10 spool exposes read/seek but no IOBase capability methods."""

        def __init__(self, contents):
            self.buffer = io.BytesIO(contents)

        def read(self, size=-1):
            return self.buffer.read(size)

        def seek(self, offset, whence=0):
            return self.buffer.seek(offset, whence)

    artifact = Artifact("content", ("doc",), kind=kind, required=True)

    artifact.validate_stream(tmp_path / "doc", Python310Spool(data))
