"""A response that lost something has to say so, in the response.

Every case here was previously a warning on stdout and an ``ok`` response
carrying a value indistinguishable from a genuinely empty artifact. The log
line is still written; what these assert is the half that a caller processing
a hundred thousand documents can actually act on.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import tarfile
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


# -----------------------------------------------------------------------------
# Report
# -----------------------------------------------------------------------------

def test_a_clean_report_has_no_entry():
    """The common case must not grow a field. A response that lost nothing is
    byte-for-byte what it was before any of this existed."""
    assert degraded.Report().entry() is None


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


def test_noting_also_logs(capsys):
    """One call, both effects. A drop site cannot record without logging or log
    without recording, because there is only the one way to do either."""
    report = degraded.Report()
    report.note(reason=degraded.UNSAFE_NAME, file="..\\escape.md")

    record = json.loads(capsys.readouterr().out.strip())
    assert record["level"] == "warning"
    assert record["reason"] == "unsafe_name"
    assert record["file"] == "..\\escape.md"
    assert report.count == 1


def test_an_unknown_reason_is_refused(capsys):
    """The vocabulary is small on purpose — these end up as metric labels and
    log filters, where a second spelling reads as a second problem."""
    with pytest.raises(ValueError, match="reason must be one of"):
        degraded.Report().note(reason="broke", file="doc.md")
    capsys.readouterr()


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


def test_an_empty_report_is_still_truthy():
    """Guards the reason `__bool__` is deliberately absent. Were it defined as
    "has anything been noted", `report or Report()` — the obvious way to write
    the default — would swap a caller's empty report for a throwaway one, and
    every drop noted afterwards would land in the discarded copy."""
    empty = degraded.Report()
    assert bool(empty) is True
    assert (empty or degraded.Report()) is empty


def test_sink_returns_the_report_it_was_given(capsys):
    report = degraded.Report()
    assert degraded.sink(report) is report
    assert isinstance(degraded.sink(None), degraded.Report)


def test_a_caller_without_a_report_still_gets_the_log_line(tmp_path, capsys):
    """Passing no report is choosing not to read the record, not turning the
    record off."""
    (tmp_path / "doc_blocks.json").write_bytes(b"{not json")
    resolve(MANIFEST, tmp_path, "doc")

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(json.loads(ln)["reason"] == "unreadable" for ln in lines)


# -----------------------------------------------------------------------------
# Artifacts
# -----------------------------------------------------------------------------

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


def test_an_intact_response_has_no_degraded_key(output_dir):
    assert "degraded" not in _entry(output_dir)


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


# -----------------------------------------------------------------------------
# Archives
# -----------------------------------------------------------------------------

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


# -----------------------------------------------------------------------------
# The key is the harness's
# -----------------------------------------------------------------------------

def test_the_degraded_key_is_reserved():
    assert degraded.ENTRY_KEY in package.RESERVED_ENTRY_KEYS


def test_metadata_may_not_claim_the_degraded_key(output_dir):
    """An engine counting its own work must not be able to overwrite the field
    that says the response is short."""
    with pytest.raises(ValueError, match=degraded.ENTRY_KEY):
        _entry(output_dir, metadata={degraded.ENTRY_KEY: "all good, honestly"})


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


# -----------------------------------------------------------------------------
# required — the artifact a response is pointless without
# -----------------------------------------------------------------------------

REQUIRED_MANIFEST = (
    Artifact("markdown", ("{basename}.md",), kind="text", required=True),
    Artifact("blocks", ("{basename}_blocks.json",), kind="json", default=[]),
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


def test_required_is_refused_on_a_collection():
    with pytest.raises(ValueError, match="required is for"):
        Artifact("images", ("images/*",), kind="b64map", required=True)


def test_required_with_a_default_is_refused():
    """The default could never be read, so one of the two is a mistake and the
    declaration does not say which."""
    with pytest.raises(ValueError, match="would never be used"):
        Artifact("markdown", ("{basename}.md",), required=True, default="")


def test_required_defaults_to_off():
    """Every manifest written before this existed keeps its behaviour."""
    assert Artifact("markdown", ("{basename}.md",)).required is False


# -----------------------------------------------------------------------------
# A glob hit the filesystem will not describe
#
# `Path.is_file()` answers False for a symlink loop and for a link to nothing —
# ELOOP and ENOENT are both in pathlib's ignored errnos — which is the same
# False it gives an ordinary directory. Dropping everything that is not a file
# therefore drops the broken ones silently, alongside the directories that were
# meant to be skipped. Artifact matching separately probes exact candidates so
# pathlib's precise selector on Python 3.10 and 3.11 cannot hide them first.
# -----------------------------------------------------------------------------

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


def test_a_real_symlink_loop_is_reported(tmp_path, capsys):
    """The faithful version of the case above, where the platform allows it."""
    _loop(tmp_path, "doc.md")

    report = degraded.Report()
    out = resolve(MANIFEST, tmp_path, "doc", keys=["markdown"], report=report)
    capsys.readouterr()

    assert out["markdown"] == ""
    assert report.entry() is not None, "a loop was dropped without a word"
    assert report.entry()["items"][0]["reason"] == "unresolvable"


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


def test_an_entry_that_cannot_be_stated_at_all_is_reported(
    output_dir, monkeypatch, capsys
):
    """`is_file()` raises rather than answering when the error is not one
    pathlib ignores — a permission error on the way to the file. That used to
    leave the exception to escape packaging as a bare OSError."""
    real_is_file = Path.is_file

    def is_file(self):
        if self.name == "doc.md":
            raise PermissionError("Permission denied")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file)

    entry = _entry(output_dir)
    capsys.readouterr()

    assert entry["markdown"] == ""
    assert entry["degraded"]["items"][0]["reason"] == "unresolvable"


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


# -----------------------------------------------------------------------------
# What a consuming worker needs to reach
# -----------------------------------------------------------------------------

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


def test_a_supplied_report_sees_archive_losses_too(output_dir, monkeypatch, capsys):
    monkeypatch.setattr(
        "runpod_doc_worker.transport.package._safe_arcname", lambda name: False
    )
    report = degraded.Report()
    _entry(output_dir, transport="tarball_b64", report=report)
    capsys.readouterr()

    assert report.count > 0
    assert {i["reason"] for i in report.entry()["items"]} == {"unsafe_name"}


def test_a_supplied_report_stays_empty_on_an_intact_job(output_dir):
    report = degraded.Report()
    entry = _entry(output_dir, report=report)

    assert report.count == 0
    assert report.entry() is None
    assert "degraded" not in entry


def test_a_report_carried_across_two_entries_accumulates(output_dir, capsys):
    """One report per job rather than per entry is a legitimate thing to want:
    a worker packaging several documents counts the job's losses, not each
    file's."""
    (output_dir / "doc_blocks.json").write_bytes(b"{not json")
    report = degraded.Report()
    _entry(output_dir, report=report)
    _entry(output_dir, report=report)
    capsys.readouterr()

    assert report.count == 2


def test_a_shared_report_does_not_contaminate_a_later_intact_entry(
    output_dir, capsys
):
    """The supplied report is job-wide, but ``degraded`` is per entry."""
    blocks = output_dir / "doc_blocks.json"
    blocks.write_bytes(b"{not json")
    report = degraded.Report()

    first = _entry(output_dir, report=report)
    blocks.write_bytes(b'[{"type": "text"}]')
    second = _entry(output_dir, report=report)
    capsys.readouterr()

    assert first["degraded"]["count"] == 1
    assert report.count == 1
    assert "degraded" not in second


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


def test_the_log_message_is_public_and_is_what_gets_logged(capsys):
    """Workers document this string to their operators as the thing to alert
    on, so it is a contract. A test here is what makes changing it fail
    something rather than silently going quiet."""
    degraded.Report().note(reason=degraded.UNREADABLE, file="doc.md")
    record = json.loads(capsys.readouterr().out.strip())

    assert degraded.MESSAGE == "response degraded"
    assert record["msg"] == degraded.MESSAGE
