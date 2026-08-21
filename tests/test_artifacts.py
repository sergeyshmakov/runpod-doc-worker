"""The artifact manifest: what a worker's output directory turns into.

These stand in for the hard-coded per-engine branches that used to do this job,
so the cases that matter are the ones that branching got right: a fallback
filename, an artifact that produced nothing, and a filter that omits a key
rather than emptying it.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

import pytest

from runpod_doc_worker.contract import degraded
from runpod_doc_worker.contract.artifacts import Artifact, keys, resolve

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


def test_reads_every_declared_artifact(output_dir):
    out = resolve(MANIFEST, output_dir, "doc")
    assert out["markdown"] == "# hello\n"
    assert out["content_list"] == [{"type": "text"}]
    assert sorted(out["images"]) == ["fig1.png", "fig2.png"]


def test_b64map_values_round_trip(output_dir):
    out = resolve(MANIFEST, output_dir, "doc")
    assert base64.b64decode(out["images"]["fig1.png"]) == b"\x89PNG\r\n\x1a\nfig1"


def test_first_matching_pattern_wins(tmp_path):
    """Later patterns are fallbacks, not additions — an engine that renamed an
    artifact between versions declares both and gets whichever it wrote."""
    (tmp_path / "doc_content_list_v2.json").write_text('["v2"]', encoding="utf-8")
    out = resolve(MANIFEST, tmp_path, "doc")
    assert out["content_list"] == ["v2"]


def test_earlier_pattern_preferred_when_both_exist(tmp_path):
    (tmp_path / "doc_content_list.json").write_text('["v1"]', encoding="utf-8")
    (tmp_path / "doc_content_list_v2.json").write_text('["v2"]', encoding="utf-8")
    out = resolve(MANIFEST, tmp_path, "doc")
    assert out["content_list"] == ["v1"]


def test_missing_artifact_yields_its_declared_default(tmp_path):
    """An empty parse must not turn a caller's `response["markdown"]` into a
    KeyError, and content_list defaults to a list rather than a dict."""
    out = resolve(MANIFEST, tmp_path, "doc")
    assert out["markdown"] == ""
    assert out["content_list"] == []
    assert out["images"] == {}


def test_derived_defaults_when_none_declared(tmp_path):
    manifest = (
        Artifact("text_thing", ("nope.txt",), kind="text"),
        Artifact("json_thing", ("nope.json",), kind="json"),
        Artifact("map_thing", ("nope/*",), kind="b64map"),
    )
    out = resolve(manifest, tmp_path, "doc")
    assert out == {"text_thing": "", "json_thing": {}, "map_thing": {}}


def test_filter_omits_keys_rather_than_emptying_them(output_dir):
    out = resolve(MANIFEST, output_dir, "doc", keys=["markdown"])
    assert out == {"markdown": "# hello\n"}
    assert "images" not in out


def test_filter_of_none_means_everything(output_dir):
    assert set(resolve(MANIFEST, output_dir, "doc", keys=None)) == {
        "markdown", "content_list", "images",
    }


def test_unknown_key_alongside_a_real_one_is_still_rejected(output_dir):
    """Deliberate change of behaviour: this previously returned the real key
    and dropped the unknown one in silence."""
    with pytest.raises(ValueError, match="not_a_thing"):
        resolve(MANIFEST, output_dir, "doc", keys=["markdown", "not_a_thing"])


def test_basename_is_substituted_into_patterns(tmp_path):
    (tmp_path / "report.md").write_text("r", encoding="utf-8")
    (tmp_path / "doc.md").write_text("d", encoding="utf-8")
    assert resolve(MANIFEST, tmp_path, "report")["markdown"] == "r"


def test_directories_never_match(tmp_path):
    """A glob that hits a directory must not be read as a file."""
    (tmp_path / "doc.md").mkdir()
    assert resolve(MANIFEST, tmp_path, "doc")["markdown"] == ""


def test_unreadable_json_falls_back_to_the_default(tmp_path):
    """A truncated artifact is the engine's problem; it must not take down a
    response that is otherwise complete."""
    (tmp_path / "doc.md").write_text("# ok\n", encoding="utf-8")
    (tmp_path / "doc_content_list.json").write_text("{not json", encoding="utf-8")
    out = resolve(MANIFEST, tmp_path, "doc")
    assert out["markdown"] == "# ok\n"
    assert out["content_list"] == []


def test_the_fallback_is_logged(tmp_path, capsys):
    """Silence is the defect, not the fallback: an unlogged substitution is a
    failure class nobody can count."""
    (tmp_path / "doc_content_list.json").write_text("{not json", encoding="utf-8")
    resolve(MANIFEST, tmp_path, "doc")
    record = json.loads(capsys.readouterr().out.strip())
    assert record["level"] == "warning"
    assert record["artifact"] == "content_list"
    assert record["error_type"] == "JSONDecodeError"


def test_undecodable_text_and_json_behave_the_same(tmp_path, capsys):
    """Same file class, same bad bytes — one kind must not fail the job while
    the other returns a default."""
    (tmp_path / "doc.md").write_bytes(b"\xff\xfe\x00bad")
    (tmp_path / "doc_content_list.json").write_bytes(b"\xff\xfe\x00bad")
    out = resolve(MANIFEST, tmp_path, "doc")
    capsys.readouterr()
    assert out["markdown"] == ""
    assert out["content_list"] == []


def test_keys_reports_declaration_order():
    assert keys(MANIFEST) == ["markdown", "content_list", "images"]


def test_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="kind must be one of"):
        Artifact("bad", ("x",), kind="parquet")


def test_rejects_an_empty_pattern_list():
    with pytest.raises(ValueError, match="at least one pattern"):
        Artifact("bad", ())


def test_rejects_a_bare_string_of_patterns():
    """A string is iterable, so this would otherwise be accepted and then fail
    deep in packaging with a format-string error."""
    with pytest.raises(ValueError, match="not a bare string"):
        Artifact("bad", "{basename}.md")


def test_rejects_an_empty_key():
    with pytest.raises(ValueError, match="non-empty string"):
        Artifact("", ("x",))


def test_rejects_a_non_string_pattern():
    with pytest.raises(ValueError, match="patterns must be strings"):
        Artifact("bad", (Path("x"),))


# -----------------------------------------------------------------------------
# Defaults are per-read, not per-process
# -----------------------------------------------------------------------------

def test_a_mutated_default_does_not_leak_into_the_next_read(tmp_path):
    """A worker process serves many jobs, and FlashBoot preserves it across
    scale-to-zero. One mutation of a shared container would poison every later
    job for the life of the process."""
    first = resolve(MANIFEST, tmp_path, "doc")
    first["content_list"].append("MUTATED")
    second = resolve(MANIFEST, tmp_path, "other")
    assert second["content_list"] == []


def test_two_json_artifacts_do_not_alias_each_other(tmp_path):
    manifest = (
        Artifact("a", ("nope_a.json",), kind="json"),
        Artifact("b", ("nope_b.json",), kind="json"),
    )
    out = resolve(manifest, tmp_path, "doc")
    out["a"]["poisoned"] = True
    assert out["b"] == {}


def test_a_declared_default_is_not_shared_across_reads(tmp_path):
    manifest = (Artifact("thing", ("nope.json",), kind="json", default={"a": [1]}),)
    first = resolve(manifest, tmp_path, "doc")
    first["thing"]["a"].append(2)
    assert resolve(manifest, tmp_path, "doc")["thing"] == {"a": [1]}


# -----------------------------------------------------------------------------
# basename is data, not pattern syntax
# -----------------------------------------------------------------------------

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


def test_engine_wildcards_in_the_pattern_still_work(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "a.png").write_bytes(b"a")
    (images / "b.png").write_bytes(b"b")
    assert sorted(resolve(MANIFEST, tmp_path, "doc")["images"]) == ["a.png", "b.png"]


def test_an_exact_broken_match_omitted_by_pathlib_is_reported(
    output_dir, monkeypatch, capsys
):
    """Python 3.10 and 3.11 ask `exists()` for an exact glob component, which
    drops the very broken target packaging needs to report. Simulate that
    selector on every supported test platform."""
    real_is_file, real_is_dir = Path.is_file, Path.is_dir
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: False if path.name == "doc.md" else real_is_file(path),
    )
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: False if path.name == "doc.md" else real_is_dir(path),
    )
    real_glob = Path.glob
    monkeypatch.setattr(
        Path,
        "glob",
        lambda path, pattern: (
            iter(()) if pattern == "doc.md" else real_glob(path, pattern)
        ),
    )

    report = degraded.Report()
    out = resolve(MANIFEST, output_dir, "doc", keys=["markdown"], report=report)
    capsys.readouterr()

    assert out["markdown"] == ""
    assert report.entry()["items"][0]["reason"] == "unresolvable"


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


def test_a_dangling_link_to_an_outside_target_degrades(tmp_path, capsys):
    job = tmp_path / "job"
    job.mkdir()
    try:
        (job / "doc.md").symlink_to(tmp_path / "never-written.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    report = degraded.Report()

    assert resolve(MANIFEST, job, "doc", keys=["markdown"], report=report) == {
        "markdown": ""
    }
    capsys.readouterr()
    assert report.entry()["items"][0]["reason"] == "unresolvable"


# -----------------------------------------------------------------------------
# Ambiguity is an error, not a silent choice
# -----------------------------------------------------------------------------

def test_a_single_value_kind_matching_two_files_raises(tmp_path):
    """Truncating to the first hit would drop a page of a document and report
    success."""
    manifest = (Artifact("markdown", ("*.md",), kind="text"),)
    (tmp_path / "page1.md").write_text("one", encoding="utf-8")
    (tmp_path / "page2.md").write_text("two", encoding="utf-8")
    with pytest.raises(ValueError, match="matched 2 files"):
        resolve(manifest, tmp_path, "doc")


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


def test_duplicate_manifest_keys_are_rejected(tmp_path):
    """keys() would advertise both while resolve() returned one."""
    manifest = (
        Artifact("markdown", ("a.md",)),
        Artifact("markdown", ("b.md",)),
    )
    with pytest.raises(ValueError, match="duplicate keys"):
        resolve(manifest, tmp_path, "doc")
    with pytest.raises(ValueError, match="duplicate keys"):
        keys(manifest)


def test_json_artifact_parses_objects_too(tmp_path):
    manifest = (Artifact("middle", ("{basename}_middle.json",), kind="json"),)
    (tmp_path / "doc_middle.json").write_text(json.dumps({"pdf_info": []}), encoding="utf-8")
    assert resolve(manifest, tmp_path, "doc")["middle"] == {"pdf_info": []}


# -----------------------------------------------------------------------------
# A basename may not steer the read out of the output directory
# -----------------------------------------------------------------------------

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


def test_a_pattern_that_escapes_the_output_dir_is_refused(tmp_path):
    """Defence in depth: the engine writes its own patterns, but none of them
    has any business reading outside the directory it was given."""
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "leak.md").write_text("ANOTHER JOB", encoding="utf-8")
    job = tmp_path / "job"
    job.mkdir()
    manifest = (Artifact("markdown", ("../other/leak.md",), kind="text"),)
    with pytest.raises(ValueError, match="outside the output directory"):
        resolve(manifest, job, "doc")


def test_a_directory_link_outside_is_classified_before_directories_are_skipped(
    tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    job = tmp_path / "job"
    job.mkdir()
    try:
        (job / "linked").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")

    manifest = (Artifact("markdown", ("linked",), kind="text"),)
    with pytest.raises(ValueError, match="outside the output directory"):
        resolve(manifest, job, "doc")


def test_an_ordinary_basename_still_resolves(tmp_path):
    (tmp_path / "report-2024_final.md").write_text("fine", encoding="utf-8")
    assert resolve(MANIFEST, tmp_path, "report-2024_final")["markdown"] == "fine"


# -----------------------------------------------------------------------------
# An unreadable member of a collection is not a failed response
# -----------------------------------------------------------------------------

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


def test_an_unknown_requested_format_is_rejected(output_dir):
    """A typo used to produce a successful response with no artifacts in it,
    which reads as 'this document produced nothing' rather than 'you asked for
    a key that does not exist'."""
    with pytest.raises(ValueError, match="markdwon"):
        resolve(MANIFEST, output_dir, "doc", keys=["markdwon"])


def test_the_error_names_what_is_available(output_dir):
    with pytest.raises(ValueError, match="content_list"):
        resolve(MANIFEST, output_dir, "doc", keys=["nope"])


def test_a_subset_of_real_keys_is_still_fine(output_dir):
    out = resolve(MANIFEST, output_dir, "doc", keys=["markdown"])
    assert set(out) == {"markdown"}


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
