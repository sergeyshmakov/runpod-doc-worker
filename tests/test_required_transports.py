"""Required-artifact enforcement across response transports."""

from __future__ import annotations

import pytest

from runpod_doc_worker.contract.artifacts import Artifact, ArtifactError
from runpod_doc_worker.transport import package


REQUIRED_MANIFEST = (
    Artifact("markdown", ("{basename}.md",), kind="text", required=True),
)


def _package(transport, output_dir, monkeypatch):
    package_s3_called = False

    def package_s3(*args, **kwargs):
        nonlocal package_s3_called
        package_s3_called = True
        return {"tarball_url": "https://example.test/result.tar.gz"}

    monkeypatch.setattr(package, "package_s3", package_s3)

    def call():
        return package.package_results_entry(
            transport=transport,
            formats=[],
            output_dir=output_dir,
            basename="doc",
            source="b64",
            manifest=REQUIRED_MANIFEST,
        )

    return call, lambda: package_s3_called


@pytest.mark.parametrize("transport", ["tarball_b64", "s3"])
def test_archive_transports_enforce_a_missing_required_artifact(
    tmp_path, monkeypatch, transport
):
    call, package_s3_called = _package(transport, tmp_path, monkeypatch)

    with pytest.raises(ArtifactError, match="required and matched no file"):
        call()
    assert package_s3_called() is False


@pytest.mark.parametrize("transport", ["tarball_b64", "s3"])
def test_archive_transports_enforce_an_unreadable_required_artifact(
    tmp_path, monkeypatch, transport, capsys
):
    (tmp_path / "doc.md").write_bytes(b"\xff\xfe\x00bad")
    call, package_s3_called = _package(transport, tmp_path, monkeypatch)

    with pytest.raises(ArtifactError, match="could not be read"):
        call()
    capsys.readouterr()
    assert package_s3_called() is False


def test_required_preflight_does_not_add_an_artifact_value(tmp_path, monkeypatch):
    (tmp_path / "doc.md").write_text("# body\n", encoding="utf-8")
    call, _ = _package("tarball_b64", tmp_path, monkeypatch)

    assert set(call()) == {"basename", "source", "tarball_b64"}
