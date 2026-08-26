"""A response that lost something has to say so, in the response. -- report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runpod_doc_worker.contract import degraded
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


def _loop(directory: Path, name: str) -> None:
    """A real two-link symlink cycle at ``name``, or skip."""
    other = directory / f"{name}.cycle"
    try:
        (directory / name).symlink_to(other)
        other.symlink_to(directory / name)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")


def test_a_clean_report_has_no_entry():
    """The common case must not grow a field. A response that lost nothing is
    byte-for-byte what it was before any of this existed."""
    assert degraded.Report().entry() is None


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


def test_an_intact_response_has_no_degraded_key(output_dir):
    assert "degraded" not in _entry(output_dir)


def test_the_degraded_key_is_reserved():
    assert degraded.ENTRY_KEY in package.RESERVED_ENTRY_KEYS


def test_metadata_may_not_claim_the_degraded_key(output_dir):
    """An engine counting its own work must not be able to overwrite the field
    that says the response is short."""
    with pytest.raises(ValueError, match=degraded.ENTRY_KEY):
        _entry(output_dir, metadata={degraded.ENTRY_KEY: "all good, honestly"})


def test_required_is_refused_on_a_collection():
    with pytest.raises(ValueError, match="required is for"):
        Artifact("images", ("images/*",), kind="b64map", required=True)


def test_required_with_a_default_is_refused():
    """The default could never be read, so one of the two is a mistake and the
    declaration does not say which."""
    with pytest.raises(ValueError, match="would never be used"):
        Artifact("markdown", ("{basename}.md",), required=True, default="")


def test_a_real_symlink_loop_is_reported(tmp_path, capsys):
    """The faithful version of the case above, where the platform allows it."""
    _loop(tmp_path, "doc.md")

    report = degraded.Report()
    out = resolve(MANIFEST, tmp_path, "doc", keys=["markdown"], report=report)
    capsys.readouterr()

    assert out["markdown"] == ""
    assert report.entry() is not None, "a loop was dropped without a word"
    assert report.entry()["items"][0]["reason"] == "unresolvable"


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


def test_the_log_message_is_public_and_is_what_gets_logged(capsys):
    """Workers document this string to their operators as the thing to alert
    on, so it is a contract. A test here is what makes changing it fail
    something rather than silently going quiet."""
    degraded.Report().note(reason=degraded.UNREADABLE, file="doc.md")
    record = json.loads(capsys.readouterr().out.strip())

    assert degraded.MESSAGE == "response degraded"
    assert record["msg"] == degraded.MESSAGE
