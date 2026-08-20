"""The artifact manifest: what a worker's output directory turns into.

These stand in for the hard-coded per-engine branches that used to do this job,
so the cases that matter are the ones that branching got right: a fallback
filename, an artifact that produced nothing, and a filter that omits a key
rather than emptying it.
"""

from __future__ import annotations

import base64
import json

import pytest

from runpod_doc_worker.contract.artifacts import Artifact, keys, resolve


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


def test_unknown_key_in_filter_is_ignored(output_dir):
    out = resolve(MANIFEST, output_dir, "doc", keys=["markdown", "not_a_thing"])
    assert out == {"markdown": "# hello\n"}


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


def test_keys_reports_declaration_order():
    assert keys(MANIFEST) == ["markdown", "content_list", "images"]


def test_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="kind must be one of"):
        Artifact("bad", ("x",), kind="parquet")


def test_rejects_an_empty_pattern_list():
    with pytest.raises(ValueError, match="at least one pattern"):
        Artifact("bad", ())


def test_json_artifact_parses_objects_too(tmp_path):
    manifest = (Artifact("middle", ("{basename}_middle.json",), kind="json"),)
    (tmp_path / "doc_middle.json").write_text(json.dumps({"pdf_info": []}), encoding="utf-8")
    assert resolve(manifest, tmp_path, "doc")["middle"] == {"pdf_info": []}
