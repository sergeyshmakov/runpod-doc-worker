"""Required-artifact enforcement across response transports."""

from __future__ import annotations

import base64
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from runpod_doc_worker.contract.artifacts import Artifact, ArtifactError
from runpod_doc_worker.transport import archive_requirements
from runpod_doc_worker.transport import package


REQUIRED_MANIFEST = (
    Artifact("markdown", ("{basename}.md",), kind="text", required=True),
)

ALL_ARCHIVES = pytest.mark.parametrize(
    "transport,archive_format",
    [
        ("tarball_b64", "tar.gz"),
        ("tarball_b64", "zip"),
        ("s3", "tar.gz"),
        ("s3", "zip"),
    ],
)


def _package(
    transport,
    output_dir,
    monkeypatch,
    archive_format="tar.gz",
    manifest=REQUIRED_MANIFEST,
):
    upload_called = False

    def package_s3(
        output_dir,
        basename,
        archive_format="tar.gz",
        report=None,
        *,
        _required_members=None,
    ):
        nonlocal upload_called
        package._build_archive_bytes(
            output_dir, archive_format, report, _required_members
        )
        upload_called = True
        return {"tarball_url": "https://example.test/result.tar.gz"}

    monkeypatch.setattr(package, "package_s3", package_s3)

    def call():
        return package.package_results_entry(
            transport=transport,
            formats=[],
            output_dir=output_dir,
            basename="doc",
            source="b64",
            manifest=manifest,
            archive_format=archive_format,
        )

    return call, lambda: upload_called


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


def test_required_archive_validation_does_not_add_an_artifact_value(
    tmp_path, monkeypatch
):
    (tmp_path / "doc.md").write_text("# body\n", encoding="utf-8")
    call, _ = _package("tarball_b64", tmp_path, monkeypatch)

    assert set(call()) == {"basename", "source", "tarball_b64"}


@ALL_ARCHIVES
def test_archive_transports_reject_an_unsafe_required_member(
    tmp_path, monkeypatch, transport, archive_format
):
    (tmp_path / "doc.md").write_text("# body\n", encoding="utf-8")
    monkeypatch.setattr(package, "_safe_arcname", lambda name: name != "doc.md")
    call, upload_called = _package(
        transport, tmp_path, monkeypatch, archive_format
    )

    with pytest.raises(ArtifactError, match="required.*cannot be archived"):
        call()
    assert upload_called() is False


@ALL_ARCHIVES
def test_archive_transports_reject_a_required_member_that_cannot_be_archived(
    tmp_path, monkeypatch, transport, archive_format
):
    required = tmp_path / "doc.md"
    required.write_text("# body\n", encoding="utf-8")
    real_read_chunk = archive_requirements._read_chunk
    read_once = False

    def read_chunk(source):
        nonlocal read_once
        if Path(source.name) == required and read_once:
            raise PermissionError("Permission denied")
        chunk = real_read_chunk(source)
        if Path(source.name) == required:
            read_once = True
        return chunk

    monkeypatch.setattr(archive_requirements, "_read_chunk", read_chunk)
    call, upload_called = _package(
        transport, tmp_path, monkeypatch, archive_format
    )

    with pytest.raises(ArtifactError, match="required.*could not be archived"):
        call()
    assert upload_called() is False


@ALL_ARCHIVES
def test_required_fallback_loss_is_reported_once_by_the_archive(
    tmp_path, monkeypatch, transport, archive_format, capsys
):
    try:
        (tmp_path / "broken.md").symlink_to(tmp_path / "never-written.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    (tmp_path / "doc.md").write_text("# body\n", encoding="utf-8")
    manifest = (
        Artifact("markdown", ("broken.md", "doc.md"), kind="text", required=True),
    )
    call, _ = _package(
        transport,
        tmp_path,
        monkeypatch,
        archive_format,
        manifest,
    )
    entry = call()

    captured = capsys.readouterr()
    assert entry["degraded"] == {
        "count": 1,
        "items": [
            {
                "artifact": None,
                "file": "broken.md",
                "reason": "unresolvable",
            }
        ],
    }
    assert captured.out.count("response degraded") == 1


def test_fatal_unresolvable_required_match_is_logged_once(
    tmp_path, monkeypatch, capsys
):
    try:
        (tmp_path / "broken.md").symlink_to(tmp_path / "never-written.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    manifest = (
        Artifact("markdown", ("broken.md",), kind="text", required=True),
    )
    call, _ = _package(
        "tarball_b64", tmp_path, monkeypatch, manifest=manifest
    )

    with pytest.raises(ArtifactError, match="required and matched no file"):
        call()
    captured = capsys.readouterr()

    assert captured.out.count("response degraded") == 1
    assert '"artifact": "markdown"' in captured.out
    assert '"reason": "unresolvable"' in captured.out


@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_required_member_is_opened_once_and_the_same_bytes_are_archived(
    tmp_path, monkeypatch, archive_format
):
    required = tmp_path / "doc.md"
    required.write_bytes(b"# one read\n")
    real_open = Path.open
    opens = 0

    def open_file(path, *args, **kwargs):
        nonlocal opens
        if path == required:
            opens += 1
            if opens > 1:
                raise PermissionError("required member was opened twice")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    call, _ = _package(
        "tarball_b64", tmp_path, monkeypatch, archive_format
    )

    raw = base64.b64decode(call()["tarball_b64"])

    assert opens == 1
    if archive_format == "zip":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert archive.read("doc.md") == b"# one read\n"
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            assert archive.extractfile("doc.md").read() == b"# one read\n"


@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_archive_validates_required_json_from_the_bytes_it_will_write(
    tmp_path, monkeypatch, archive_format
):
    (tmp_path / "doc.json").write_bytes(b"{")
    manifest = (
        Artifact("content", ("doc.json",), kind="json", required=True),
    )
    call, _ = _package(
        "tarball_b64",
        tmp_path,
        monkeypatch,
        archive_format,
        manifest,
    )

    with pytest.raises(ArtifactError, match="JSONDecodeError"):
        call()
