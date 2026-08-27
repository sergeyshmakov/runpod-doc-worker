"""`ALLOW_LOCAL_FETCH` is an operator's exemption, not a caller's.

The bypass exists for *documents*: an operator serving PDFs from a private mirror
sets it, and the address policy stops refusing hosts that resolve somewhere
non-routable. It is scoped to the whole worker, which is right for a URL the
operator chose and wrong for a URL a *caller* supplies in a job payload.

Both consumers of this package shipped the consequence. With the bypass on — a
supported, documented configuration — any job could point the worker's model-server
field at 169.254.169.254 and the check said nothing, so the worker would POST to
cloud metadata from inside its own network. One of them had a comment stating the
behaviour was intentional, reasoning that an operator who sets the variable has
opted in; they had opted in to fetching their own documents.

`allow_local=False` refuses the bypass for one call, so a caller-supplied URL can
be held to the policy while the operator's own document fetches keep their
exemption.
"""

from __future__ import annotations

import pytest

from runpod_doc_worker import config
from runpod_doc_worker.transport import net

PRIVATE = {
    "metadata.internal": ["169.254.169.254"],
    "loopback.internal": ["127.0.0.1"],
    "rfc1918.internal": ["10.0.0.5"],
    "public.example": ["93.184.216.34"],
}


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch: pytest.MonkeyPatch):
    """Resolution is stubbed so nothing here touches DNS or opens a socket. A test
    that lets one of these through would otherwise sit in a connect timeout, which
    is how a suite here once went from 3s to 24s."""
    def addresses(host, port):  # noqa: ARG001
        return PRIVATE.get(host, [])

    monkeypatch.setattr(net, "_addresses_for", addresses)


@pytest.fixture
def bypass_on(monkeypatch: pytest.MonkeyPatch):
    """The operator's documented setting for a private document mirror."""
    monkeypatch.setenv(
        config.active().env_name(net.ALLOW_LOCAL_FETCH), "1"
    )


@pytest.mark.parametrize("host", ["metadata.internal", "loopback.internal", "rfc1918.internal"])
def test_a_private_target_is_refused_by_default(host: str) -> None:
    """The baseline, so the tests below are about the bypass and not about the
    policy failing to work at all."""
    with pytest.raises(ValueError, match="publicly routable"):
        net.check_target(f"http://{host}/v1", field="server_url")


@pytest.mark.parametrize("host", ["metadata.internal", "loopback.internal", "rfc1918.internal"])
def test_the_bypass_still_exempts_a_document_fetch(host: str, bypass_on) -> None:
    """What the variable is for. An operator with a private mirror keeps it."""
    net.check_target(f"http://{host}/report.pdf", field="file_url")


@pytest.mark.parametrize("host", ["metadata.internal", "loopback.internal", "rfc1918.internal"])
def test_the_bypass_does_not_reach_a_field_that_refuses_it(
    host: str, bypass_on
) -> None:
    """The fix. This is the exact request both consumers accepted: bypass on, and a
    caller naming an internal address for the model server."""
    with pytest.raises(ValueError, match="publicly routable"):
        net.check_target(
            f"http://{host}/v1", field="server_url", allow_local=False
        )


def test_a_public_target_passes_either_way(bypass_on) -> None:
    """The guard on the narrowing: refusing the bypass must not refuse everything.
    A check that always raised would satisfy the test above."""
    net.check_target("http://public.example/v1", field="server_url", allow_local=False)
    net.check_target("http://public.example/v1", field="server_url")


def test_allow_local_true_overrides_an_operator_who_did_not_set_it() -> None:
    """The parameter is an override in both directions, and a caller passing True
    should get the exemption without the environment. Asserted so the three-state
    default is not quietly a two-state one."""
    net.check_target("http://rfc1918.internal/x", field="file_url", allow_local=True)


def test_the_default_still_consults_the_environment(bypass_on) -> None:
    """`None` means "ask the operator", which is what every existing caller relies
    on. If this regressed to False, every worker with the bypass set would start
    refusing its own document mirror."""
    net.check_target("http://rfc1918.internal/report.pdf", field="file_url")


def test_the_message_still_names_the_escape_hatch() -> None:
    """A refusal that does not say what to set leaves an operator guessing -- and
    for `allow_local=False` the hint is still worth printing, since the operator
    reading it may be the one who set the variable and expected it to apply."""
    with pytest.raises(ValueError) as caught:
        net.check_target(
            "http://rfc1918.internal/v1", field="server_url", allow_local=False
        )
    assert "ALLOW_LOCAL_FETCH" in str(caught.value)


def test_resolve_checked_returns_the_addresses_it_validated() -> None:
    """The lower-level call keeps its contract: the caller gets every address, in
    resolver order, so a connection can be pinned to what was checked."""
    assert net.resolve_checked(
        "http://public.example/v1", field="server_url", allow_local=False
    ) == ["93.184.216.34"]
