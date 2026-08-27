"""The cap check is actually called by the packaging path.

The first version of this feature added `exceeds_gateway_cap` and
`oversized_response_error`, tested both thoroughly, and never called either. A
repository-wide search found them referenced only by their own unit tests -- so a
commit titled "refuse a response the gateway would silently discard" refused
nothing, and the outputs it was written to protect were still being dropped.

Sixteen passing tests said the logic was right. None of them said anything was
using it. These do.
"""

from __future__ import annotations

import pytest

from runpod_doc_worker.contract import artifacts as _artifacts
from runpod_doc_worker.transport import package, response_size

MB = 1024 * 1024

MANIFEST = (_artifacts.Artifact("markdown", ("{basename}.md",), kind="text"),)


@pytest.fixture(autouse=True)
def _restore_module_state(monkeypatch: pytest.MonkeyPatch):
    """Both are module globals a consumer is invited to set, so a test that moves
    one must not leave it moved for everything that runs after."""
    monkeypatch.setattr(response_size, "MAX_RESPONSE_MB", response_size.MAX_RESPONSE_MB)
    monkeypatch.setattr(response_size, "BULKY_ARTIFACT", None)


def _output(tmp_path, size_bytes: int):
    out = tmp_path / "out"
    out.mkdir()
    (out / "doc.md").write_text("x" * size_bytes, encoding="utf-8")
    return out


def test_an_oversized_entry_is_refused_rather_than_returned(tmp_path) -> None:
    """The test the first version of this feature was missing."""
    output_dir = _output(tmp_path, 2 * MB)
    monkey = response_size.MAX_RESPONSE_MB
    try:
        response_size.MAX_RESPONSE_MB = 1  # 1 MB cap, so 2 MB of markdown is over
        with pytest.raises(response_size.ResponseTooLargeError) as caught:
            package.package_results_entry(
                transport="inline",
                formats=["markdown"],
                output_dir=output_dir,
                basename="doc",
                source="test",
                manifest=MANIFEST,
            )
    finally:
        response_size.MAX_RESPONSE_MB = monkey
    message = str(caught.value)
    assert "MB" in message and "start_page" in message, (
        "the refusal has to carry the measured size and the way out"
    )


def test_an_ordinary_entry_is_returned_untouched(tmp_path) -> None:
    """The guard on the guard: the check must not refuse normal responses, which
    is the failure mode of getting the margin or the measurement wrong."""
    entry = package.package_results_entry(
        transport="inline",
        formats=["markdown"],
        output_dir=_output(tmp_path, 32),
        basename="doc",
        source="test",
        manifest=MANIFEST,
    )
    assert entry["markdown"] == "x" * 32
    assert entry["basename"] == "doc"


def test_the_refusal_names_the_configured_bulky_artifact(tmp_path) -> None:
    """`BULKY_ARTIFACT` is how a consumer makes the message specific to its own
    output. If nothing reads it, setting it looks like it works and does nothing."""
    output_dir = _output(tmp_path, 2 * MB)
    monkey = response_size.MAX_RESPONSE_MB
    try:
        response_size.MAX_RESPONSE_MB = 1
        response_size.BULKY_ARTIFACT = "middle.json"
        with pytest.raises(response_size.ResponseTooLargeError) as caught:
            package.package_results_entry(
                transport="inline",
                formats=["markdown"],
                output_dir=output_dir,
                basename="doc",
                source="test",
                manifest=MANIFEST,
            )
    finally:
        response_size.MAX_RESPONSE_MB = monkey
    assert "middle.json" in str(caught.value)


def test_raising_the_cap_disables_the_refusal(tmp_path) -> None:
    """The documented escape hatch for a deployment that is not behind the cap.
    A knob that does not work is worse than no knob."""
    output_dir = _output(tmp_path, 2 * MB)
    monkey = response_size.MAX_RESPONSE_MB
    try:
        response_size.MAX_RESPONSE_MB = 10_000
        entry = package.package_results_entry(
            transport="inline",
            formats=["markdown"],
            output_dir=output_dir,
            basename="doc",
            source="test",
            manifest=MANIFEST,
        )
    finally:
        response_size.MAX_RESPONSE_MB = monkey
    assert len(entry["markdown"]) == 2 * MB


def test_the_measurement_counts_a_base64_payload(tmp_path) -> None:
    """tarball_b64 is the transport most likely to trip this, and the one where the
    payload is a single long string rather than many fields."""
    entry = {"basename": "doc", "tarball_b64": "A" * (3 * MB)}
    measured = response_size.measure_entry_bytes(entry)
    assert measured >= 3 * MB
    assert response_size.exceeds_gateway_cap(measured, transport="tarball_b64") is False
    assert response_size.exceeds_gateway_cap(measured * 10, transport="tarball_b64")


def test_the_measurement_walks_nested_structures() -> None:
    """Inline entries carry dicts of images and lists of blocks, and a measurement
    that only looked at top-level strings would read them as nearly empty."""
    flat = response_size.measure_entry_bytes({"a": "x" * 100})
    nested = response_size.measure_entry_bytes(
        {"images": {"one.png": "x" * 100, "two.png": "x" * 100}}
    )
    assert nested > flat


def test_an_s3_entry_is_never_refused(tmp_path, monkeypatch) -> None:
    """s3 returns a URL, so its entry cannot be too large -- and refusing one for
    size would be refusing the remedy the other messages recommend."""
    entry = {"basename": "doc", "tarball_url": "https://bucket.example/o.tar.gz"}
    monkeypatch.setattr(response_size, "MAX_RESPONSE_MB", 0)
    response_size.refuse_if_undeliverable(entry, transport="s3")
