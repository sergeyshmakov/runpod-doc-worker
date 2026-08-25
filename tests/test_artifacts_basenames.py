"""The artifact manifest: what a worker's output directory turns into. -- basenames."""

from __future__ import annotations

import json
import sys

import pytest

from runpod_doc_worker.contract import degraded
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


def test_basename_is_substituted_into_patterns(tmp_path):
    (tmp_path / "report.md").write_text("r", encoding="utf-8")
    (tmp_path / "doc.md").write_text("d", encoding="utf-8")
    assert resolve(MANIFEST, tmp_path, "report")["markdown"] == "r"


def test_rejects_a_bare_string_of_patterns():
    """A string is iterable, so this would otherwise be accepted and then fail
    deep in packaging with a format-string error."""
    with pytest.raises(ValueError, match="not a bare string"):
        Artifact("bad", "{basename}.md")


@pytest.mark.parametrize("basename", ["report[2024]", "[!x]", "a[b]c"])
def test_glob_metacharacters_in_basename_resolve_literally(tmp_path, basename):
    (tmp_path / f"{basename}.md").write_text("real content", encoding="utf-8")
    assert resolve(MANIFEST, tmp_path, basename)["markdown"] == "real content"


@posix_only
@pytest.mark.parametrize("basename", ["doc*", "a?b"])
def test_wildcard_metacharacters_in_basename_resolve_literally(tmp_path, basename):
    (tmp_path / f"{basename}.md").write_text("real content", encoding="utf-8")
    assert resolve(MANIFEST, tmp_path, basename)["markdown"] == "real content"


@posix_only
def test_a_wildcard_basename_does_not_match_another_document(tmp_path):
    """Unescaped, `doc*.md` would match docA.md and return another job's text."""
    (tmp_path / "doc*.md").write_text("the literal one", encoding="utf-8")
    (tmp_path / "docA.md").write_text("someone else", encoding="utf-8")
    assert resolve(MANIFEST, tmp_path, "doc*")["markdown"] == "the literal one"


def test_overlapping_patterns_report_one_unresolvable_path(
    output_dir, monkeypatch, capsys
):
    manifest = (Artifact("markdown", ("{basename}.md", "*.md")),)
    monkeypatch.setattr(
        "runpod_doc_worker.paths.kind",
        lambda path: "unresolvable" if path.name == "doc.md" else "file",
    )
    report = degraded.Report()

    assert resolve(manifest, output_dir, "doc", report=report) == {"markdown": ""}
    log_lines = capsys.readouterr().out.strip().splitlines()
    assert report.entry()["count"] == 1
    assert len(report.entry()["items"]) == 1
    assert len(log_lines) == 1


def test_json_artifact_parses_objects_too(tmp_path):
    manifest = (Artifact("middle", ("{basename}_middle.json",), kind="json"),)
    (tmp_path / "doc_middle.json").write_text(json.dumps({"pdf_info": []}), encoding="utf-8")
    assert resolve(manifest, tmp_path, "doc")["middle"] == {"pdf_info": []}


@pytest.mark.parametrize("basename", [
    "../other/doc",
    "..\other\doc",
    "sub/doc",
    "sub\doc",
    "..",
])
def test_a_basename_with_path_components_is_rejected(tmp_path, basename):
    """Escaping glob metacharacters does not neutralise a separator: `..` and
    `/` survive it, so `{basename}.md` could read an adjacent job's output."""
    with pytest.raises(ValueError, match="basename"):
        resolve(MANIFEST, tmp_path, basename)


def test_traversal_cannot_reach_an_adjacent_directory(tmp_path):
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "doc.md").write_text("ANOTHER JOB", encoding="utf-8")
    job = tmp_path / "job"
    job.mkdir()
    with pytest.raises(ValueError, match="basename"):
        resolve(MANIFEST, job, "../other/doc")


def test_an_ordinary_basename_still_resolves(tmp_path):
    (tmp_path / "report-2024_final.md").write_text("fine", encoding="utf-8")
    assert resolve(MANIFEST, tmp_path, "report-2024_final")["markdown"] == "fine"
