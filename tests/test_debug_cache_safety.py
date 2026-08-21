"""Cache-location and containment contracts for the filesystem probe."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from runpod_doc_worker import config
from runpod_doc_worker.obs import debug


class _DirectoryEntry:
    def __init__(self, path: Path) -> None:
        self.name = path.name
        self.path = str(path)

    @staticmethod
    def is_dir(*, follow_symlinks=True):
        return True


def test_resolve_snapshot_path_reports_a_truncated_fallback_scan(tmp_path, monkeypatch):
    model = tmp_path / "models--acme--parser"
    snapshots = model / "snapshots"
    snapshots.mkdir(parents=True)

    real_scan = debug._scan

    def truncated_scan(directory, max_entries):
        if directory == snapshots:
            return [], True
        return real_scan(directory, max_entries)

    monkeypatch.setattr(debug, "_scan", truncated_scan)

    out = debug._resolve_snapshot_path(tmp_path, "acme/parser")
    assert out["resolved_path"] is None
    assert "truncated" in str(out["issue"])


def test_find_model_dir_does_not_select_a_symlinked_snapshot(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    model = hub / "models--acme--parser"
    snapshots = model / "snapshots"
    snapshots.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    class SymlinkedDirectory:
        name = "linked"
        path = str(outside)

        @staticmethod
        def is_dir(*, follow_symlinks=True):
            return follow_symlinks

    real_scan = debug._scan

    def scan_with_link(directory, max_entries):
        if directory == snapshots:
            return [SymlinkedDirectory()], False
        return real_scan(directory, max_entries)

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    monkeypatch.setattr(debug, "_scan", scan_with_link)
    debug.find_model_dir.cache_clear()
    try:
        assert debug.find_model_dir() == str(model)
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_find_model_dir_marks_a_candidate_from_a_truncated_hub_scan(
    tmp_path, monkeypatch
):
    hub = tmp_path / "hub"
    model = hub / "models--acme--parser"
    (model / "snapshots" / "abc123").mkdir(parents=True)

    real_scan = debug._scan

    def truncated_hub_scan(directory, max_entries):
        if directory == hub:
            return [_DirectoryEntry(model)], True
        return real_scan(directory, max_entries)

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    monkeypatch.setattr(debug, "_scan", truncated_hub_scan)
    debug.find_model_dir.cache_clear()
    try:
        result = debug.find_model_dir()
        assert str(model) in result
        assert "truncated" in result
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_find_model_dir_marks_a_candidate_from_a_truncated_snapshot_scan(
    tmp_path, monkeypatch
):
    hub = tmp_path / "hub"
    model = hub / "models--acme--parser"
    snapshots = model / "snapshots"
    snapshot = snapshots / "abc123"
    snapshot.mkdir(parents=True)

    real_scan = debug._scan

    def truncated_snapshot_scan(directory, max_entries):
        if directory == snapshots:
            return [_DirectoryEntry(snapshot)], True
        return real_scan(directory, max_entries)

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    monkeypatch.setattr(debug, "_scan", truncated_snapshot_scan)
    debug.find_model_dir.cache_clear()
    try:
        result = debug.find_model_dir()
        assert str(snapshot) in result
        assert "truncated" in result
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_find_model_dir_does_not_scan_a_snapshots_directory_outside_the_model(
    tmp_path, monkeypatch
):
    hub = tmp_path / "hub"
    model = hub / "models--acme--parser"
    snapshots = model / "snapshots"
    (snapshots / "abc123").mkdir(parents=True)

    real_within = debug._paths.within
    real_scan = debug._scan

    def snapshots_escape(root: Path, candidate: Path) -> bool:
        if root == model and candidate == snapshots:
            return False
        return real_within(root, candidate)

    def refuse_external_scan(directory, max_entries):
        if directory == snapshots:
            raise AssertionError("scanned a snapshots directory outside the model")
        return real_scan(directory, max_entries)

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    monkeypatch.setattr(debug._paths, "within", snapshots_escape)
    monkeypatch.setattr(debug, "_scan", refuse_external_scan)
    debug.find_model_dir.cache_clear()
    try:
        result = debug.find_model_dir()
        assert str(model) in result
        assert "outside" in result
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_probe_readers_reject_a_snapshots_directory_outside_the_model(
    tmp_path, monkeypatch
):
    hub = tmp_path / "hub"
    model = hub / "models--acme--parser"
    snapshots = model / "snapshots"
    (snapshots / "abc123").mkdir(parents=True)

    real_within = debug._paths.within

    def snapshots_escape(root: Path, candidate: Path) -> bool:
        if root == model and candidate == snapshots:
            return False
        return real_within(root, candidate)

    monkeypatch.setattr(debug._paths, "within", snapshots_escape)

    resolved = debug._resolve_snapshot_path(hub, "acme/parser")
    assert resolved["resolved_path"] is None
    assert resolved["snapshot_subdirs"] == []
    assert "outside" in str(resolved["issue"])
    assert debug._snapshot_names(model) == ([], False)


def test_snapshot_resolver_does_not_read_refs_main_outside_the_model(
    tmp_path, monkeypatch
):
    hub = tmp_path / "hub"
    model = hub / "models--acme--parser"
    refs_main = model / "refs" / "main"
    refs_main.parent.mkdir(parents=True)
    refs_main.write_text("abc123", encoding="utf-8")
    (model / "snapshots" / "abc123").mkdir(parents=True)

    real_within = debug._paths.within

    def refs_escape(root: Path, candidate: Path) -> bool:
        if root == model and candidate == refs_main:
            return False
        return real_within(root, candidate)

    monkeypatch.setattr(debug._paths, "within", refs_escape)

    resolved = debug._resolve_snapshot_path(hub, "acme/parser")
    assert resolved["refs_main_content"] is None
    assert resolved["resolved_path"] is None
    assert "outside" in str(resolved["issue"])


def test_snapshot_fallback_marks_a_candidate_from_a_truncated_scan(
    tmp_path, monkeypatch
):
    model = tmp_path / "models--acme--parser"
    snapshots = model / "snapshots"
    snapshot = snapshots / "abc123"
    snapshot.mkdir(parents=True)

    real_scan = debug._scan

    def truncated_snapshot_scan(directory, max_entries):
        if directory == snapshots:
            return [_DirectoryEntry(snapshot)], True
        return real_scan(directory, max_entries)

    monkeypatch.setattr(debug, "_scan", truncated_snapshot_scan)

    resolved = debug._resolve_snapshot_path(tmp_path, "acme/parser")
    assert resolved["resolved_path"] == str(snapshot)
    assert "truncated" in str(resolved["issue"])


def test_snapshot_fallback_rechecks_candidate_containment(tmp_path, monkeypatch):
    model = tmp_path / "models--acme--parser"
    snapshots = model / "snapshots"
    snapshot = snapshots / "abc123"
    snapshot.mkdir(parents=True)

    real_within = debug._paths.within

    def snapshot_escapes(root: Path, candidate: Path) -> bool:
        if root == snapshots and candidate == snapshot:
            return False
        return real_within(root, candidate)

    monkeypatch.setattr(debug._paths, "within", snapshot_escapes)

    resolved = debug._resolve_snapshot_path(tmp_path, "acme/parser")
    assert resolved["resolved_path"] is None
    assert "outside" in str(resolved["issue"])


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks need privileges on Windows")
def test_every_probe_reader_rejects_a_symlinked_snapshots_directory(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    (outside / "abc123").mkdir(parents=True)
    hub = tmp_path / "hub"
    model = hub / "models--acme--parser"
    model.mkdir(parents=True)
    (model / "snapshots").symlink_to(outside, target_is_directory=True)

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    debug.find_model_dir.cache_clear()
    try:
        assert "outside" in debug.find_model_dir()
        resolved = debug._resolve_snapshot_path(hub, "acme/parser")
        assert "outside" in str(resolved["issue"])
        assert debug._snapshot_names(model) == ([], False)
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


@pytest.mark.skipif(sys.platform == "win32", reason="file symlinks need privileges on Windows")
def test_snapshot_resolver_rejects_a_symlinked_refs_main(tmp_path):
    outside = tmp_path / "outside-ref"
    outside.write_text("abc123", encoding="utf-8")
    model = tmp_path / "hub" / "models--acme--parser"
    refs = model / "refs"
    refs.mkdir(parents=True)
    (refs / "main").symlink_to(outside)
    (model / "snapshots" / "abc123").mkdir(parents=True)

    resolved = debug._resolve_snapshot_path(tmp_path / "hub", "acme/parser")
    assert resolved["refs_main_content"] is None
    assert resolved["resolved_path"] is None
    assert "outside" in str(resolved["issue"])


def test_hf_hub_cache_overrides_hf_home_for_finding_and_probing(tmp_path, monkeypatch):
    hub = tmp_path / "direct-hub-cache"
    snapshot = hub / "models--acme--parser" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)

    monkeypatch.setenv("HF_HOME", str(tmp_path / "unused-home"))
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    config.configure(
        config.WorkerConfig(
            model_globs=("models--acme--*",),
            probe_model_ids=("acme/parser",),
        )
    )
    debug.find_model_dir.cache_clear()
    try:
        assert debug.find_model_dir() == str(snapshot)
        probe = debug.probe_filesystem()
        assert probe["resolution_attempts"][0]["resolved_path"] == str(snapshot)
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_legacy_hub_cache_is_used_below_hf_hub_cache(tmp_path, monkeypatch):
    legacy_hub = tmp_path / "legacy-hub"
    snapshot = legacy_hub / "models--acme--parser" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)

    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(legacy_hub))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "unused-home"))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    debug.find_model_dir.cache_clear()
    try:
        assert debug.find_model_dir() == str(snapshot)
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_hub_cache_paths_expand_environment_variables(tmp_path, monkeypatch):
    hub = tmp_path / "expanded-hub"
    snapshot = hub / "models--acme--parser" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)

    monkeypatch.setenv("RUNPOD_DOC_WORKER_TEST_CACHE", str(tmp_path))
    monkeypatch.setenv(
        "HF_HUB_CACHE", "$RUNPOD_DOC_WORKER_TEST_CACHE/expanded-hub"
    )
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    debug.find_model_dir.cache_clear()
    try:
        assert debug.find_model_dir() == str(snapshot)
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_xdg_cache_home_is_used_when_hugging_face_vars_are_absent(tmp_path, monkeypatch):
    hub = tmp_path / "xdg" / "huggingface" / "hub"
    snapshot = hub / "models--acme--parser" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)

    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    debug.find_model_dir.cache_clear()
    try:
        assert debug.find_model_dir() == str(snapshot)
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_a_truncated_hub_scan_with_no_usable_match_is_not_reported_as_absence(
    tmp_path, monkeypatch
):
    hub = tmp_path / "hub"
    model = hub / "models--acme--parser"
    model.mkdir(parents=True)

    real_scan = debug._scan

    def truncated_hub_scan(directory, max_entries):
        if directory == hub:
            return [_DirectoryEntry(model)], True
        return real_scan(directory, max_entries)

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    monkeypatch.setattr(debug, "_scan", truncated_hub_scan)
    monkeypatch.setattr(debug, "_newest", lambda paths: None)
    debug.find_model_dir.cache_clear()
    try:
        result = debug.find_model_dir()
        assert result is not None
        assert "truncated" in result
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_model_search_marks_a_truncated_snapshot_listing(tmp_path, monkeypatch):
    model = tmp_path / "models--acme--parser"
    snapshots = model / "snapshots"
    snapshots.mkdir(parents=True)

    real_scan = debug._scan

    def truncated_snapshot_scan(directory, max_entries):
        if directory == snapshots:
            return [], True
        return real_scan(directory, max_entries)

    monkeypatch.setattr(debug, "_scan", truncated_snapshot_scan)

    found, note = debug.find_model_dirs(tmp_path)
    assert note is None
    assert found[0]["snapshots"] == []
    assert found[0]["snapshots_truncated"] is True


def test_snapshot_resolver_rejects_a_model_root_outside_the_hub(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    model = hub / "models--acme--parser"
    (model / "snapshots" / "abc123").mkdir(parents=True)

    real_within = debug._paths.within

    def model_root_escapes(root: Path, candidate: Path) -> bool:
        if root == hub and candidate == model:
            return False
        return real_within(root, candidate)

    monkeypatch.setattr(debug._paths, "within", model_root_escapes)

    out = debug._resolve_snapshot_path(hub, "acme/parser")
    assert out["model_root_exists"] is False
    assert out["resolved_path"] is None
    assert "outside" in str(out["issue"])


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks need privileges on Windows")
def test_snapshot_resolver_rejects_a_symlinked_model_root(tmp_path):
    outside = tmp_path / "outside"
    (outside / "snapshots" / "abc123").mkdir(parents=True)
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "models--acme--parser").symlink_to(outside, target_is_directory=True)

    out = debug._resolve_snapshot_path(hub, "acme/parser")
    assert out["model_root_exists"] is False
    assert out["resolved_path"] is None
    assert "outside" in str(out["issue"])
