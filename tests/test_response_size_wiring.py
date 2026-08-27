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
    """These are module globals a consumer is invited to set, so a test that moves
    one must not leave it moved for everything that runs after."""
    monkeypatch.setattr(response_size, "MAX_RESPONSE_MB", response_size.MAX_RESPONSE_MB)
    monkeypatch.setattr(response_size, "BULKY_ARTIFACT", None)
    monkeypatch.setattr(response_size, "ENFORCE_RESPONSE_CAP", False)


@pytest.fixture
def enforcing(monkeypatch: pytest.MonkeyPatch):
    """Enforcement on and a 1 MB cap, which is what a consumer opts into."""
    monkeypatch.setattr(response_size, "ENFORCE_RESPONSE_CAP", True)
    monkeypatch.setattr(response_size, "MAX_RESPONSE_MB", 1)


def _output(tmp_path, size_bytes: int):
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "doc.md").write_text("x" * size_bytes, encoding="utf-8")
    return out


def _entry(tmp_path, size_bytes: int = 32, **kwargs):
    return package.package_results_entry(
        transport="inline",
        formats=["markdown"],
        output_dir=_output(tmp_path, size_bytes),
        basename="doc",
        source="test",
        manifest=MANIFEST,
        **kwargs,
    )


def test_nothing_is_refused_unless_a_consumer_opts_in(tmp_path) -> None:
    """The compatibility guarantee. Adopting the release that adds this must not
    change what an existing worker returns -- a library should not begin raising
    from a version bump, so enforcement is off until a consumer turns it on.
    """
    entry = _entry(tmp_path, 2 * MB)
    assert len(entry["markdown"]) == 2 * MB


def test_an_oversized_entry_is_refused_rather_than_returned(
    tmp_path, enforcing
) -> None:
    """The test the first version of this feature was missing."""
    with pytest.raises(response_size.ResponseTooLargeError) as caught:
        _entry(tmp_path, 2 * MB)
    message = str(caught.value)
    assert "MB" in message and "start_page" in message, (
        "the refusal has to carry the measured size and the way out"
    )


def test_an_ordinary_entry_is_returned_untouched(tmp_path, enforcing) -> None:
    """The guard on the guard, with enforcement on: the check must not refuse
    normal responses, which is the failure mode of getting the margin or the
    measurement wrong."""
    entry = _entry(tmp_path, 32)
    assert entry["markdown"] == "x" * 32
    assert entry["basename"] == "doc"


def test_the_refusal_names_the_configured_bulky_artifact(
    tmp_path, enforcing, monkeypatch
) -> None:
    """`BULKY_ARTIFACT` is how a consumer makes the message specific to its own
    output. If nothing reads it, setting it looks like it works and does nothing."""
    monkeypatch.setattr(response_size, "BULKY_ARTIFACT", "middle.json")
    with pytest.raises(response_size.ResponseTooLargeError) as caught:
        _entry(tmp_path, 2 * MB)
    assert "middle.json" in str(caught.value)


def test_two_entries_that_each_fit_can_still_be_refused_together(
    tmp_path, enforcing
) -> None:
    """The gateway measures the whole body, so a per-entry check misses exactly the
    case it exists for: two entries of half the cap each pass alone and the response
    they form does not. One budget spans them."""
    budget = response_size.ResponseBudget(transport="inline")
    first = _entry(tmp_path / "a", 600 * 1024, budget=budget)
    assert first["basename"] == "doc"
    with pytest.raises(response_size.ResponseTooLargeError):
        _entry(tmp_path / "b", 600 * 1024, budget=budget)


def test_each_response_gets_its_own_budget(tmp_path, enforcing) -> None:
    """A worker with a concurrency modifier runs several jobs in one process, so a
    shared counter would refuse whichever job happened to arrive last. Two separate
    budgets of the same size both pass."""
    for name in ("a", "b"):
        entry = _entry(
            tmp_path / name,
            600 * 1024,
            budget=response_size.ResponseBudget(transport="inline"),
        )
        assert entry["basename"] == "doc"


def test_the_budget_counts_the_handler_envelope(tmp_path) -> None:
    """The debug block and results wrapper are part of the body the gateway
    measures, so a budget starts already partly spent."""
    budget = response_size.ResponseBudget(transport="inline")
    assert budget.used > 0
    generous = response_size.ResponseBudget(transport="inline", envelope=0)
    assert generous.used == 0


def test_raising_the_cap_disables_the_refusal(
    tmp_path, enforcing, monkeypatch
) -> None:
    """The documented escape hatch for a deployment that is not behind the cap --
    a proxy in front of the worker, results read some other way. A knob that does
    not work is worse than no knob."""
    monkeypatch.setattr(response_size, "MAX_RESPONSE_MB", 10_000)
    entry = _entry(tmp_path, 2 * MB)
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
