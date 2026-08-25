"""Input-source resolution: which files and which URLs the worker accepts. -- inline."""

from __future__ import annotations

import asyncio
import socket

import pytest

from runpod_doc_worker.transport import io as worker_io
from runpod_doc_worker.transport import net as worker_net


def _resolve(job_input: dict):
    return asyncio.run(worker_io.resolve_input_bytes(job_input))


def _stub_resolver(monkeypatch, mapping: dict[str, list[str]]) -> None:
    """Resolve hosts from a dict instead of DNS."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port or 80))
            for addr in mapping[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


_PROXY_VARS = (
    "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
)


def _clear_proxy_env(monkeypatch):
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


def _transport_chosen_for(monkeypatch, url: str):
    """Which transport the client would use for `url`, as httpx selects it."""
    import httpx

    client = httpx.AsyncClient(
        timeout=1.0,
        transport=worker_net.CheckedTargetTransport(field="file_url"),
        mounts=worker_net.environment_proxy_mounts(),
        event_hooks={"request": [worker_net.request_hook]},
    )
    try:
        return client._transport_for_url(httpx.URL(url))
    finally:
        asyncio.run(client.aclose())


def test_volume_roots_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("WORKER_VOLUME_ROOTS", raising=False)
    assert [str(p) for p in worker_io.volume_roots()] == [
        str(worker_io.Path(r)) for r in worker_io.DEFAULT_VOLUME_ROOTS
    ]


def test_default_roots_cover_the_places_any_worker_receives_a_file():
    """The network-volume mount, wherever an operator mounted it, and the
    per-job temp tree. Dropping one is a contract change rather than a
    tidy-up. A path that exists because some image put it there is not here —
    that belongs in a worker's own `volume_roots`."""
    assert set(worker_io.DEFAULT_VOLUME_ROOTS) == {
        "/runpod-volume", "/workspace", "/tmp",
    }


def test_volume_roots_env_replaces_defaults(monkeypatch, tmp_path):
    other = tmp_path / "other"
    monkeypatch.setenv("WORKER_VOLUME_ROOTS", f"{tmp_path}, {other} ,")
    assert [str(p) for p in worker_io.volume_roots()] == [str(tmp_path), str(other)]


def test_volume_roots_blank_env_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("WORKER_VOLUME_ROOTS", "   ")
    assert len(worker_io.volume_roots()) == len(worker_io.DEFAULT_VOLUME_ROOTS)


def test_resolve_input_bytes_checks_the_url_before_connecting(monkeypatch):
    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {"docs.internal": ["10.0.0.7"]})
    with pytest.raises(ValueError, match="publicly routable"):
        _resolve({"file_url": "http://docs.internal/report.pdf"})


def test_resolve_input_bytes_rejects_a_non_http_url():
    with pytest.raises(ValueError, match="must be an http"):
        _resolve({"file_url": "file:///etc/hosts"})


def test_a_proxied_pattern_goes_through_the_proxy(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    chosen = _transport_chosen_for(monkeypatch, "http://cdn.example.com/a.pdf")
    assert not isinstance(chosen, worker_net.CheckedTargetTransport)


def test_a_scheme_with_no_proxy_stays_on_the_checked_path(monkeypatch):
    """Only HTTP_PROXY is set, so an https fetch is not proxied at all — it
    must not lose the checked transport on the way past the mounts."""
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    chosen = _transport_chosen_for(monkeypatch, "https://cdn.example.com/a.pdf")
    assert isinstance(chosen, worker_net.CheckedTargetTransport)


def test_a_no_proxy_host_stays_on_the_checked_path(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "cdn.example.com")
    chosen = _transport_chosen_for(monkeypatch, "http://cdn.example.com/a.pdf")
    assert isinstance(chosen, worker_net.CheckedTargetTransport)


def test_without_a_proxy_everything_is_on_the_checked_path(monkeypatch):
    _clear_proxy_env(monkeypatch)
    for url in ("https://cdn.example.com/a.pdf", "http://cdn.example.com/a.pdf"):
        assert isinstance(
            _transport_chosen_for(monkeypatch, url),
            worker_net.CheckedTargetTransport,
        )


def test_httpx_environment_proxy_helper_guard():
    """Guard: environment_proxy_mounts leans on a private httpx helper.

    Reimplementing NO_PROXY matching would be the more fragile option, so the
    dependency is explicit and checked here instead.
    """
    from httpx._utils import get_environment_proxies

    assert callable(get_environment_proxies)
    assert isinstance(get_environment_proxies(), dict)


def test_the_request_hook_still_rejects_a_bad_scheme():
    import httpx

    with pytest.raises(ValueError, match="file_url must be an http"):
        asyncio.run(worker_net.request_hook(
            httpx.Request("GET", "ftp://cdn.example.com/r.pdf")
        ))


def test_url_host_is_left_alone_so_pooling_stays_per_hostname(monkeypatch):
    """Two hostnames sharing an address must not share a connection.

    Connections are pooled by URL origin and the TLS handshake is performed
    against the host in it. Substituting the address into the URL would collapse
    two hostnames into one origin, and the second would be served over the
    first one's connection without a handshake of its own.
    """
    transport = worker_net.CheckedTargetTransport(field="file_url")
    try:
        assert isinstance(
            transport._pool._network_backend, worker_net.CheckedAddressBackend
        )
        # The transport does not touch requests, so httpx keeps building origins
        # from the caller's hostname.
        assert not hasattr(transport, "_pin")
        assert "handle_async_request" not in vars(worker_net.CheckedTargetTransport)
    finally:
        asyncio.run(transport.aclose())


def test_a_prompt_download_is_unaffected(monkeypatch):
    """The budget must not fire on a download that simply takes a moment."""
    class _Fast:
        headers = {}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"%PDF-1.4 fine"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *a, **k):
            return _Fast()

    monkeypatch.setattr(worker_io.httpx, "AsyncClient", _Client)
    raw, src = _resolve({"file_url": "https://cdn.example.com/quick.pdf"})
    assert raw == b"%PDF-1.4 fine"
    assert src == "url:https://cdn.example.com/quick.pdf"


def test_the_outer_budget_does_not_disturb_an_ordinary_fetch(monkeypatch):
    async def quick(file_url):
        return b"%PDF-1.4 fine", f"url:{file_url}"

    monkeypatch.setattr(worker_io, "_fetch_url", quick)
    raw, src = _resolve({"file_url": "https://cdn.example.com/quick.pdf"})
    assert raw == b"%PDF-1.4 fine"


@pytest.mark.parametrize("payload", [
    "!!!!",
    "not base64 at all",
    "@@@@@@@@",
])
def test_a_non_base64_payload_is_rejected(payload):
    """b64decode discards non-alphabet characters by default, so this returned
    empty or corrupted bytes and reported a successful fetch."""
    with pytest.raises(ValueError, match="not valid base64"):
        _resolve({"file_b64": payload})


def test_a_payload_with_stray_characters_is_rejected():
    """The dangerous case: enough valid base64 that the corrupted decode still
    looks like a document, so nothing downstream notices."""
    import base64 as _b64
    good = _b64.b64encode(b"%PDF-1.4 real document").decode()
    corrupt = good[:8] + "!!!" + good[8:]
    with pytest.raises(ValueError, match="not valid base64"):
        _resolve({"file_b64": corrupt})


def test_a_valid_payload_still_decodes():
    import base64 as _b64
    payload = _b64.b64encode(b"%PDF-1.4 hello").decode()
    raw, src = _resolve({"file_b64": payload})
    assert raw == b"%PDF-1.4 hello"
    assert src == "b64"


@pytest.mark.parametrize("sep", ["\n", "\r\n", " ", "\t"])
def test_line_wrapped_base64_is_still_accepted(sep):
    """Encoders wrap base64, and the size ceiling in this module already
    assumes they do. Validating without normalising whitespace first would
    reject input that has always worked."""
    import base64 as _b64
    encoded = _b64.b64encode(b"%PDF-1.4 hello world padding here").decode()
    wrapped = sep.join(encoded[i:i + 8] for i in range(0, len(encoded), 8))
    raw, _ = _resolve({"file_b64": wrapped})
    assert raw == b"%PDF-1.4 hello world padding here"


def test_base64_padding_errors_are_reported_as_such():
    import base64 as _b64
    encoded = _b64.b64encode(b"%PDF-1.4 hello").decode().rstrip("=")[:-1]
    with pytest.raises(ValueError, match="not valid base64"):
        _resolve({"file_b64": encoded})


def test_the_rejection_survives_a_configured_proxy(monkeypatch):
    """The proxy path does not use the checked transport, so this has to be
    refused before the request is handed over."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    with pytest.raises(ValueError, match="dotted-quad"):
        _resolve({"file_url": "http://2130706433/doc.pdf"})


@pytest.mark.parametrize("payload", ["   ", "\n\n", "\t", " \r\n \t "])
def test_a_whitespace_only_payload_is_rejected(payload):
    """`""` is falsy and fails the source check, while `"   "` is truthy and
    survived it, then normalised to nothing and decoded to empty bytes — a
    successful fetch of no document. Two spellings of the same caller mistake
    should not end differently."""
    with pytest.raises(ValueError, match="no base64 data"):
        _resolve({"file_b64": payload})


def test_the_empty_and_whitespace_cases_both_refuse_to_return_bytes():
    """Whatever the wording, neither may report a successful fetch."""
    for payload in ("", "   "):
        with pytest.raises(ValueError):
            _resolve({"file_b64": payload})


@pytest.mark.parametrize("addr", [
    "224.0.0.1",        # all-hosts
    "239.255.255.250",  # SSDP
    "ff02::1",          # IPv6 all-nodes
    "ff05::c",          # IPv6 site-local
])
def test_multicast_answers_are_not_routable(addr):
    """`is_global` reports multicast as global, and it was the one category
    this predicate got wrong — an audit of every other class (broadcast,
    unspecified, reserved, documentation, CGNAT, loopback, private) found them
    already rejected."""
    assert worker_net._is_routable(addr) is False


@pytest.mark.parametrize("addr", ["93.184.216.34", "2606:4700:4700::1111"])
def test_ordinary_public_addresses_stay_routable(addr):
    assert worker_net._is_routable(addr) is True


def test_a_mapped_multicast_answer_is_also_refused():
    """The mapped form must be judged the same as the address it wraps."""
    assert worker_net._is_routable("::ffff:224.0.0.1") is False


def test_the_inline_ceiling_is_reachable_and_is_the_one_enforced():
    """A worker asserting the refusal has to build a string that exceeds it,
    and the alternative is copying the arithmetic into a test — where it stops
    describing this function the first time the headroom changes."""
    ceiling = worker_io.max_inline_b64_chars()
    assert ceiling > worker_io.MAX_INLINE_FILE_MB * 1024 * 1024

    with pytest.raises(ValueError, match="inline file too large"):
        asyncio.run(worker_io.resolve_input_bytes({"file_b64": "A" * (ceiling + 1)}))
