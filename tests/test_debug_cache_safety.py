"""Cache-location and containment contracts for the filesystem probe."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from runpod_doc_worker import config
from runpod_doc_worker.obs import debug


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
