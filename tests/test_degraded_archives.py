"""A response that lost something has to say so, in the response. -- archives."""

from __future__ import annotations

import base64
import io
import sys
import tarfile
from pathlib import Path

import pytest

from runpod_doc_worker.contract import degraded
from runpod_doc_worker.contract.artifacts import Artifact
from runpod_doc_worker.transport import package

MANIFEST = (
    Artifact("markdown", ("{basename}.md",), kind="text"),
    Artifact("blocks", ("{basename}_blocks.json",), kind="json", default=[]),
    Artifact("images", ("images/*",), kind="b64map"),
)


@pytest.fixture
def output_dir(tmp_path):
    (tmp_path / "doc.md").write_bytes(b"# hello\n")
    (tmp_path / "doc_blocks.json").write_bytes(b'[{"type": "text"}]')
    images = tmp_path / "images"
    images.mkdir()
    (images / "fig1.png").write_bytes(b"\x89PNG\r\n\x1a\nfig1")
    return tmp_path


def _entry(output_dir, transport="inline", **kwargs):
    return package.package_results_entry(
        transport=transport,
        formats=["markdown", "blocks", "images"],
        output_dir=output_dir,
        basename="doc",
        source="b64",
        manifest=MANIFEST,
        **kwargs,
    )


REQUIRED_MANIFEST = (
    Artifact("markdown", ("{basename}.md",), kind="text", required=True),
    Artifact("blocks", ("{basename}_blocks.json",), kind="json", default=[]),
)


def _undescribable(monkeypatch, name: str) -> None:
    """Make one entry answer False to both type questions, as ELOOP does."""
    real_is_file, real_is_dir = Path.is_file, Path.is_dir
    monkeypatch.setattr(
        Path, "is_file", lambda self: False if self.name == name else real_is_file(self)
    )
    monkeypatch.setattr(
        Path, "is_dir", lambda self: False if self.name == name else real_is_dir(self)
    )


def _loop(directory: Path, name: str) -> None:
    """A real two-link symlink cycle at ``name``, or skip."""
    other = directory / f"{name}.cycle"
    try:
        (directory / name).symlink_to(other)
        other.symlink_to(directory / name)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")


def test_an_archive_member_left_outside_is_reported(output_dir, tmp_path, capsys):
    """A tarball short of a file the engine wrote is otherwise indistinguishable
    from one the engine never wrote."""
    outside = tmp_path.parent / "elsewhere.md"
    outside.write_bytes(b"ANOTHER JOB")
    try:
        (output_dir / "link.md").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")

    entry = _entry(output_dir, transport="tarball_b64")
    capsys.readouterr()

    raw = base64.b64decode(entry["tarball_b64"])
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        assert "link.md" not in {m.name for m in tar.getmembers()}

    (item,) = entry["degraded"]["items"]
    assert item["file"] == "link.md"
    assert item["reason"] == "outside_output_dir"
    assert item["artifact"] is None  # an archive member is not a manifest key


def test_an_escaping_member_reports_the_escape_on_every_platform(
    output_dir, monkeypatch, capsys
):
    """The symlink case above needs POSIX to set up. This asserts the same
    reporting where a real one cannot be created, so the branch is not covered
    only on the CI runner."""
    monkeypatch.setattr(
        "runpod_doc_worker.paths.relation",
        lambda root, candidate: (
            "outside" if candidate.name == "doc.md" else "inside"
        ),
    )
    entry = _entry(output_dir, transport="tarball_b64")
    capsys.readouterr()

    (item,) = entry["degraded"]["items"]
    assert item == {"artifact": None, "file": "doc.md", "reason": "outside_output_dir"}


def test_a_member_the_filesystem_will_not_place_is_reported_as_such(
    output_dir, monkeypatch, capsys
):
    """Not as an escape. This is the whole point of relation() having three
    answers: a symlink loop is not evidence of a traversal, and reporting it as
    one sends a reader hunting something that never happened."""
    real_resolve = Path.resolve

    def resolve(self, *args, **kwargs):
        if self.name == "doc.md":
            raise RuntimeError("Symlink loop from 'doc.md'")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    entry = _entry(output_dir, transport="tarball_b64")
    capsys.readouterr()

    (item,) = entry["degraded"]["items"]
    assert item["file"] == "doc.md"
    assert item["reason"] == "unresolvable"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="backslash is not a legal filename character on Windows",
)
def test_an_unsafely_named_member_is_reported(output_dir, capsys):
    # A legal POSIX filename that an extractor reads as a path. The reason is
    # covered on every platform by the archive-parity test below, which reaches
    # the same branch without needing the filename.
    (output_dir / "..\\escape.md").write_bytes(b"payload")

    entry = _entry(output_dir, transport="tarball_b64")
    capsys.readouterr()

    (item,) = entry["degraded"]["items"]
    assert item["reason"] == "unsafe_name"


def test_a_zip_reports_the_same_omissions_as_a_tarball(output_dir, monkeypatch, capsys):
    """Both containers take their members from the same list, so both have to
    report the same losses — otherwise the answer depends on the container."""
    monkeypatch.setattr(
        "runpod_doc_worker.transport.package._safe_arcname", lambda name: False
    )
    tar_entry = _entry(output_dir, transport="tarball_b64")
    zip_entry = _entry(output_dir, transport="tarball_b64", archive_format="zip")
    capsys.readouterr()

    assert tar_entry["degraded"]["count"] == zip_entry["degraded"]["count"] > 0
    assert tar_entry["degraded"]["items"] == zip_entry["degraded"]["items"]


def test_an_intact_archive_has_no_degraded_key(output_dir):
    assert "degraded" not in _entry(output_dir, transport="tarball_b64")


def test_an_archive_member_the_filesystem_will_not_describe_is_reported(
    output_dir, monkeypatch, capsys
):
    _undescribable(monkeypatch, "doc.md")
    entry = _entry(output_dir, transport="tarball_b64")
    capsys.readouterr()

    assert entry["degraded"]["items"][0]["file"] == "doc.md"
    assert entry["degraded"]["items"][0]["reason"] == "unresolvable"


def test_a_real_symlink_loop_is_reported_by_the_archive(output_dir, capsys):
    _loop(output_dir, "loop.md")

    entry = _entry(output_dir, transport="tarball_b64")
    capsys.readouterr()

    assert "degraded" in entry, "a loop was left out of the archive without a word"
    assert {i["reason"] for i in entry["degraded"]["items"]} == {"unresolvable"}


def test_a_supplied_report_sees_archive_losses_too(output_dir, monkeypatch, capsys):
    monkeypatch.setattr(
        "runpod_doc_worker.transport.package._safe_arcname", lambda name: False
    )
    report = degraded.Report()
    _entry(output_dir, transport="tarball_b64", report=report)
    capsys.readouterr()

    assert report.count > 0
    assert {i["reason"] for i in report.entry()["items"]} == {"unsafe_name"}
