"""A response that lost something has to say so, in the response. -- artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runpod_doc_worker.contract import artifacts, degraded
from runpod_doc_worker.contract.artifacts import Artifact, resolve
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


def test_a_noted_drop_appears_with_its_cause(capsys):
    report = degraded.Report()
    report.note(
        reason=degraded.UNREADABLE,
        file="doc.md",
        artifact="markdown",
        error_type="UnicodeDecodeError",
    )
    capsys.readouterr()

    assert report.entry() == {
        "count": 1,
        "items": [{
            "artifact": "markdown",
            "file": "doc.md",
            "reason": "unreadable",
            "error_type": "UnicodeDecodeError",
        }],
    }


def test_a_truncated_report_still_counts_everything(capsys):
    """A pathological output directory must not enumerate itself into the
    response — but a list that stops has to say it stopped, or the response
    reads as complete."""
    report = degraded.Report()
    for i in range(degraded.MAX_ITEMS + 25):
        report.note(reason=degraded.UNREADABLE, file=f"fig{i}.png", artifact="images")
    capsys.readouterr()

    entry = report.entry()
    assert entry["count"] == degraded.MAX_ITEMS + 25
    assert len(entry["items"]) == degraded.MAX_ITEMS


def test_the_reported_items_are_a_copy(capsys):
    """A response is handed to a caller, and a worker process serves many jobs."""
    report = degraded.Report()
    report.note(reason=degraded.UNREADABLE, file="doc.md", artifact="markdown")
    capsys.readouterr()

    first = report.entry()
    first["items"].append("junk")
    assert len(report.entry()["items"]) == 1


def test_an_unreadable_artifact_is_reported_not_just_defaulted(output_dir, capsys):
    """The value is the same empty list it always was. The difference is that a
    caller can now tell it apart from a document with no blocks in it."""
    (output_dir / "doc_blocks.json").write_bytes(b"{not json")
    entry = _entry(output_dir)
    capsys.readouterr()

    assert entry["blocks"] == []
    assert entry["degraded"]["count"] == 1
    (item,) = entry["degraded"]["items"]
    assert item["artifact"] == "blocks"
    assert item["reason"] == "unreadable"
    assert item["error_type"] == "JSONDecodeError"


def test_an_undecodable_text_artifact_is_reported(output_dir, capsys):
    (output_dir / "doc.md").write_bytes(b"\xff\xfe\x00bad")
    entry = _entry(output_dir)
    capsys.readouterr()

    assert entry["markdown"] == ""
    assert entry["degraded"]["items"][0]["artifact"] == "markdown"


def test_a_dropped_collection_member_is_reported(output_dir, monkeypatch, capsys):
    """The other members still ship — that is the point of the per-member
    fallback. What was missing is the line saying one of them did not."""
    bad = output_dir / "images" / "fig2.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\nfig2")
    real_read_bytes = Path.read_bytes

    def read_bytes(self):
        if self.name == "fig2.png":
            raise OSError("Input/output error")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    entry = _entry(output_dir)
    capsys.readouterr()

    assert set(entry["images"]) == {"fig1.png"}
    assert entry["degraded"]["items"][0]["file"] == "fig2.png"
    assert entry["degraded"]["items"][0]["artifact"] == "images"


def test_a_match_the_filesystem_will_not_place_degrades_rather_than_failing(
    output_dir, monkeypatch, capsys
):
    """It used to raise, blaming a traversal: `within()` answered False for both
    "outside the directory" and "could not be resolved", and only the first had
    a message. A job died over a symlink loop, described as reading whatever sat
    next to its output."""
    real_resolve = Path.resolve

    def resolve(self, *args, **kwargs):
        if self.name == "doc.md":
            raise RuntimeError("Symlink loop from 'doc.md'")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    entry = _entry(output_dir)
    capsys.readouterr()

    assert entry["markdown"] == ""  # the default, not a failed job
    (item,) = entry["degraded"]["items"]
    assert item["artifact"] == "markdown"
    assert item["reason"] == "unresolvable"


def test_several_drops_are_all_reported(output_dir, capsys):
    (output_dir / "doc.md").write_bytes(b"\xff\xfe\x00bad")
    (output_dir / "doc_blocks.json").write_bytes(b"{not json")
    entry = _entry(output_dir)
    capsys.readouterr()

    assert entry["degraded"]["count"] == 2
    assert {i["artifact"] for i in entry["degraded"]["items"]} == {"markdown", "blocks"}


def test_a_filtered_out_artifact_cannot_degrade_the_response(output_dir, capsys):
    """An artifact nobody asked for is not read, so its state is not this
    response's problem."""
    (output_dir / "doc_blocks.json").write_bytes(b"{not json")
    entry = package.package_results_entry(
        transport="inline",
        formats=["markdown"],
        output_dir=output_dir,
        basename="doc",
        source="b64",
        manifest=MANIFEST,
    )
    capsys.readouterr()
    assert "degraded" not in entry


def test_a_manifest_may_not_claim_the_degraded_key(output_dir):
    with pytest.raises(ValueError, match=degraded.ENTRY_KEY):
        package.package_results_entry(
            transport="inline",
            formats=[degraded.ENTRY_KEY],
            output_dir=output_dir,
            basename="doc",
            source="b64",
            manifest=(Artifact(degraded.ENTRY_KEY, ("{basename}.md",)),),
        )


def test_a_required_artifact_that_is_absent_raises(tmp_path):
    """Reporting a degradation is the right answer for a part. It is the wrong
    answer for the whole point of the job."""
    with pytest.raises(artifacts.ArtifactError, match="required and matched no file"):
        resolve(REQUIRED_MANIFEST, tmp_path, "doc")


def test_a_required_artifact_that_is_unreadable_raises(tmp_path, capsys):
    (tmp_path / "doc.md").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(artifacts.ArtifactError, match="could not be read"):
        resolve(REQUIRED_MANIFEST, tmp_path, "doc")
    # Still logged: the response will not survive to carry the reason, so the
    # log line is the only place an operator can read it.
    assert json.loads(capsys.readouterr().out.strip())["reason"] == "unreadable"


def test_the_error_names_the_patterns_it_tried(tmp_path):
    with pytest.raises(artifacts.ArtifactError, match=r"\{basename\}\.md"):
        resolve(REQUIRED_MANIFEST, tmp_path, "doc")


def test_an_optional_sibling_still_degrades_normally(tmp_path, capsys):
    """Requiring one artifact must not make the others fatal."""
    (tmp_path / "doc.md").write_text("# ok\n", encoding="utf-8")
    (tmp_path / "doc_blocks.json").write_bytes(b"{not json")
    report = degraded.Report()
    out = resolve(REQUIRED_MANIFEST, tmp_path, "doc", report=report)
    capsys.readouterr()

    assert out["markdown"] == "# ok\n"
    assert out["blocks"] == []
    assert report.entry()["items"][0]["artifact"] == "blocks"


def test_a_required_artifact_is_not_raised_over_when_it_is_filtered_out(tmp_path):
    """`formats` decides what is read. An artifact nobody asked for cannot be
    missing from a response it was never going into."""
    (tmp_path / "doc_blocks.json").write_bytes(b"[]")
    out = resolve(REQUIRED_MANIFEST, tmp_path, "doc", keys=["blocks"])
    assert out == {"blocks": []}


def test_the_error_type_is_distinct_from_a_declaration_error(tmp_path):
    """A worker telling its caller "your input was bad" apart from "my engine
    produced nothing" needs these to be different exceptions."""
    assert issubclass(artifacts.ArtifactError, RuntimeError)
    assert not issubclass(artifacts.ArtifactError, ValueError)


def test_required_defaults_to_off():
    """Every manifest written before this existed keeps its behaviour."""
    assert Artifact("markdown", ("{basename}.md",)).required is False


def test_an_artifact_the_filesystem_will_not_describe_is_reported(
    output_dir, monkeypatch, capsys
):
    _undescribable(monkeypatch, "doc.md")
    entry = _entry(output_dir)
    capsys.readouterr()

    assert entry["markdown"] == ""
    (item,) = entry["degraded"]["items"]
    assert item["artifact"] == "markdown"
    assert item["reason"] == "unresolvable"


def test_a_directory_matching_a_pattern_is_still_skipped_in_silence(tmp_path, capsys):
    """Only the broken ones are worth reporting. A directory named like an
    artifact is an ordinary thing an engine does, and reporting it would teach
    a caller to ignore the field."""
    (tmp_path / "doc.md").mkdir()
    report = degraded.Report()
    out = resolve(MANIFEST, tmp_path, "doc", keys=["markdown"], report=report)
    capsys.readouterr()

    assert out["markdown"] == ""
    assert report.entry() is None


def test_a_caller_can_supply_the_report(output_dir, capsys):
    """A worker that counts degradations should not have to read a response
    back to find out what it lost. The entry still carries the field."""
    (output_dir / "doc_blocks.json").write_bytes(b"{not json")
    report = degraded.Report()
    entry = _entry(output_dir, report=report)
    capsys.readouterr()

    assert report.count == 1
    assert report.entry()["items"][0]["artifact"] == "blocks"
    assert entry["degraded"] == report.entry()


def test_a_shared_report_does_not_share_mutable_items_with_an_entry(
    output_dir, capsys
):
    """Consumer edits to an entry must not rewrite the job-wide report."""
    (output_dir / "doc_blocks.json").write_bytes(b"{not json")
    report = degraded.Report()

    entry = _entry(output_dir, report=report)
    capsys.readouterr()
    entry["degraded"]["items"][0]["artifact"] = "consumer-rewrite"

    assert report.entry()["items"][0]["artifact"] == "blocks"
