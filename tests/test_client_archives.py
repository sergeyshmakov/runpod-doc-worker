"""Container detection and the `extract` entry point."""

from __future__ import annotations

import base64
import gzip
import subprocess
import sys
from pathlib import Path

import pytest
from runpod_doc_client import (
    ResponseError,
    archives,
    errors,
    extract,
    fetch,
    limits,
    names,
    tarballs,
    zips,
)
from runpod_doc_client.limits import MAX_METADATA_BYTES

from tests.client_fixtures import (
    _oversized_pax_gzip,
    _tar_of,
    _tar_with,
    _zip64_archive,
)


def test_the_default_decoder_would_have_silently_returned_nothing() -> None:
    """The reason this function exists, pinned as a fact about the stdlib rather
    than left as a claim in a comment."""
    assert base64.b64decode("!!!!") == b""


def test_a_body_that_is_not_an_archive_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ResponseError, match="could not be read"):
        extract(b"this was never a tar", tmp_path)


def test_a_corrupt_zip_is_refused(tmp_path: Path) -> None:
    """Reachable because `extract` picks the container from the leading bytes of
    whatever actually arrived, not from the requested archive format."""
    with pytest.raises(ResponseError, match="could not be read"):
        extract(b"PK\x03\x04 truncated right after the signature", tmp_path)


def test_a_traversing_tar_member_is_refused(tmp_path: Path) -> None:
    """CVE-2007-4559. Checked before extraction rather than relying on the stdlib
    filter, so the guarantee does not depend on the Python patch release."""
    with pytest.raises(ResponseError, match="escapes the destination"):
        extract(_tar_with("../escaped.txt"), tmp_path)


def test_a_non_regular_tar_member_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ResponseError, match="not a regular file or dir"):
        extract(_tar_with("link", kind="symlink"), tmp_path)


def test_a_destination_that_cannot_be_created_is_refused(tmp_path: Path) -> None:
    """`dest_dir` naming an existing regular file raises from `mkdir`, before
    either archive helper runs — outside the contract despite being inside the
    public call."""
    blocker = tmp_path / "afile"
    blocker.write_text("i am a file", encoding="utf-8")

    with pytest.raises(ResponseError, match="destination could not be created"):
        extract(b"junk", blocker)


def test_importing_the_client_does_not_load_worker_modules() -> None:
    """The boundary, asserted in a fresh interpreter.

    It used to be a claim about imports only: `runpod_doc_client` was
    `runpod_doc_worker.client`, so importing it ran the root package initializer
    and pulled in `runpod_doc_worker.config` every time. Now the two are separate
    distributions and the invariant is the stronger one -- no `runpod_doc_worker`
    module is loaded at all, and none is even installed in a client-only
    environment.

    Run in a subprocess because this test session has already imported both.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, runpod_doc_client; "
            "print(sorted(m for m in sys.modules if m.startswith('runpod_doc_worker')))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", (
        f"importing the client loaded worker modules: {result.stdout.strip()}"
    )


def test_the_client_imports_nothing_outside_the_standard_library() -> None:
    """And the property the separate distribution exists to deliver.

    Lazy imports could keep httpx out of `sys.modules`; only a separate
    distribution keeps it out of the environment. This checks the first half,
    which is the part a test can see -- the second is `client/pyproject.toml`
    declaring no dependencies, asserted below.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, runpod_doc_client; "
            "print(sorted(m for m in sys.modules "
            "if m.split('.')[0] in {'httpx', 'httpcore', 'anyio', 'boto3', 'runpod'}))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", (
        f"the client pulled in third-party modules: {result.stdout.strip()}"
    )


def test_the_client_distribution_declares_no_dependencies() -> None:
    """The half no import check can see: what pip installs.

    A consumer depending on `runpod-doc-worker` gets httpx whatever the imports
    do, because the worker side declares it. This is the line that makes the
    client's environment lean, so it is the line worth pinning.
    """
    manifest = (
        Path(__file__).resolve().parents[1] / "client" / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert "dependencies = []" in manifest, (
        "the client distribution must declare no runtime dependencies"
    )

def test_the_lazy_root_exports_still_work() -> None:
    """Making the re-exports lazy must not change the worker-side API."""
    import runpod_doc_worker

    assert runpod_doc_worker.WorkerConfig is not None
    assert callable(runpod_doc_worker.configure)
    assert callable(runpod_doc_worker.active)
    assert "WorkerConfig" in dir(runpod_doc_worker)
    with pytest.raises(AttributeError):
        runpod_doc_worker.no_such_name


@pytest.mark.parametrize("payload", ["not bytes", 12345, ["a"], {"a": 1}])
def test_an_archive_that_is_not_bytes_is_refused(payload: object, tmp_path: Path) -> None:
    """`io.BytesIO(data)` raises a bare TypeError, and `extract` is exported
    directly."""
    with pytest.raises(ResponseError, match="should be bytes"):
        extract(payload, tmp_path)  # type: ignore[arg-type]


@pytest.mark.parametrize("dest", [12345, None, object()])
def test_a_destination_that_is_not_a_path_is_refused(dest: object) -> None:
    """`Path(dest_dir)` raises TypeError before the guarded `mkdir` is reached, so
    this sat outside the contract even though it happened inside the public
    call."""
    with pytest.raises(ResponseError, match="not a usable path"):
        extract(b"x", dest)  # type: ignore[arg-type]


def test_a_destination_with_a_nul_is_refused() -> None:
    """A NUL survives `Path()` and `resolve()` and is rejected by `mkdir` as
    `ValueError: embedded null character in path` — not an OSError, so guarding
    the resolve alone left it escaping. The reproduction caught that; the first
    version of this fix did not."""
    with pytest.raises(ResponseError):
        extract(b"x", "bad\0dir")


@pytest.mark.parametrize("payload", [memoryview(b"abcdef")[::2], memoryview(b"abcdef")[::-1]])
def test_a_strided_memoryview_is_handled(payload: object, tmp_path: Path) -> None:
    """The type check advertised `memoryview`, but BytesIO needs a contiguous
    buffer, so a strided view reached archive detection and raised BufferError.
    It is normalised rather than rejected, since a contiguous view is genuinely
    fine and the copy only happens for the odd case — so this surfaces as an
    ordinary "not an archive" refusal."""
    with pytest.raises(ResponseError):
        extract(payload, tmp_path)  # type: ignore[arg-type]


def test_the_module_no_longer_touches_the_umask() -> None:
    """Structural, because the race is invisible in a single-threaded test: the
    only safe amount of `os.umask` in this package is none."""
    source = "".join(
        Path(module.__file__).read_text(encoding="utf-8")
        for module in (archives, fetch, names, tarballs, zips)
    )
    assert "os.umask" not in source


def test_a_destination_that_is_a_symlink_loop_is_refused(tmp_path: Path) -> None:
    """`extract`'s resolve guard omitted RuntimeError while `within` had it - the
    same call, two guards, one narrower."""
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(loop)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    with pytest.raises(ResponseError):
        extract(b"junk", loop)


def test_a_tar_declaring_too_many_members_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quotas were enforced only by the zip path, so the protection depended
    on which container the worker happened to send."""
    monkeypatch.setattr(limits, "MAX_ARCHIVE_MEMBERS", 10)
    with pytest.raises(ResponseError, match="members"):
        extract(_tar_of([f"f{i}.txt" for i in range(50)]), tmp_path)


def test_a_compressed_tar_declaring_a_huge_expansion_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compressed tar hides the ratio exactly as a zip does: the reproduction
    was a 541-byte gzip expanding to 50 KB."""
    monkeypatch.setattr(limits, "MAX_EXTRACTED_BYTES", 1000)
    data = _tar_of([f"f{i}.txt" for i in range(50)], size=1000, mode="w:gz")
    assert len(data) < 5000, "the archive itself must be small"
    with pytest.raises(ResponseError, match="expands to"):
        extract(data, tmp_path)


def test_ordinary_member_paths_still_extract(tmp_path: Path) -> None:
    """The constraint: a nested path with an extension must stay ordinary."""
    extract(_tar_of(["doc.md", "images/fig.jpg"], size=4), tmp_path)
    assert (tmp_path / "doc.md").is_file()
    assert (tmp_path / "images" / "fig.jpg").is_file()


def test_tar_quotas_abort_during_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`getmembers()` decompresses the whole stream and materialises every
    TarInfo before either quota can be read, so a tiny archive declaring millions
    of headers exhausts memory before `len(members)` is reached. Walking
    incrementally bounds the cost by the quota instead of by the archive."""
    monkeypatch.setattr(limits, "MAX_ARCHIVE_MEMBERS", 5)
    with pytest.raises(ResponseError, match="members"):
        extract(_tar_of([f"f{i}.txt" for i in range(200)]), tmp_path)


def test_the_size_quota_also_aborts_during_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "MAX_EXTRACTED_BYTES", 500)
    with pytest.raises(ResponseError, match="expands to"):
        extract(_tar_of([f"f{i}.txt" for i in range(20)], size=100, mode="w:gz"), tmp_path)


def test_the_decompression_tuple_tracks_the_interpreter() -> None:
    """Zip method 93 is Zstandard, supported from 3.14, and a malformed payload
    raises ZstdError — neither an OSError nor any of the others, so it escaped the
    way zlib.error and LZMAError each did in turn.

    Asserted as a property of the running interpreter rather than a fixed list, so
    the test says the same thing on every supported release.
    """
    names = {error.__name__ for error in errors._DECOMPRESSION_ERRORS}
    assert {"error", "LZMAError", "EOFError"} <= names
    try:
        from compression.zstd import ZstdError  # noqa: F401
    except ImportError:
        assert "ZstdError" not in names
    else:
        assert "ZstdError" in names


def test_a_zip64_self_extracting_archive_cannot_bypass_the_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consequence of the above, which is the part that matters: returning
    None skipped the member cap, so the layout that defeated the arithmetic was
    also the layout that got past the limit."""
    monkeypatch.setattr(limits, "MAX_ARCHIVE_MEMBERS", 5)
    payload = b"MZ" + b"stub" * 500 + _zip64_archive(end_record=True)
    with pytest.raises(ResponseError, match="over"):
        extract(payload, tmp_path)


def test_detection_does_not_read_metadata_the_bound_would_refuse(
    tmp_path: Path,
) -> None:
    """The bound was installed by `tarinfo=`, so it applied to extraction and not
    to detection -- and detection parses the first member, which is where the
    oversized block sits.

    Measured before the fix: a 2,180-byte gzip decompressed 2,098,688 bytes inside
    `is_tarfile` alone, and only then did the limit refuse it. The refusal was
    real and the allocation had already happened, which makes the bound decorative
    on exactly the input that defeats it.
    """
    payload = _oversized_pax_gzip()
    assert len(payload) < 8192, "the fixture must be small to make the point"

    decompressed = 0
    real_read = gzip.GzipFile.read

    def counting_read(self, size=-1):
        nonlocal decompressed
        chunk = real_read(self, size)
        decompressed += len(chunk)
        return chunk

    gzip.GzipFile.read = counting_read
    try:
        with pytest.raises(ResponseError, match="metadata"):
            extract(payload, tmp_path)
    finally:
        gzip.GzipFile.read = real_read

    assert decompressed < MAX_METADATA_BYTES + 65536, (
        f"decompressed {decompressed} bytes before refusing; the bound has to "
        f"apply to detection as well as extraction"
    )
