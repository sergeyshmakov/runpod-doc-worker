"""Input-source resolution: which files and which URLs the worker accepts. -- network. -- sockets."""

from __future__ import annotations

import asyncio
import socket
import time

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


class _CannedStream:
    """A network stream that replays canned response bytes.

    Enough of httpcore's stream surface for it to parse a real response, so a
    redirect can be driven through the actual client stack — the transport, the
    connection pool and CheckedAddressBackend — rather than around it.
    """

    def __init__(self, payload: bytes) -> None:
        self._data = payload

    async def read(self, max_bytes, timeout=None):
        chunk, self._data = self._data[:max_bytes], self._data[max_bytes:]
        return chunk

    async def write(self, buffer, timeout=None):
        return None

    async def aclose(self):
        return None

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self

    def get_extra_info(self, info):
        return None


def _stub_socket_backend(monkeypatch, on_connect):
    """Answer connect_tcp below CheckedAddressBackend, recording the address."""
    import httpcore

    async def fake_connect_tcp(self, host, port, timeout=None, local_address=None,
                               socket_options=None):
        return on_connect(host, port)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", fake_connect_tcp)


_PROXY_VARS = (
    "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
)


def _clear_proxy_env(monkeypatch):
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


def test_every_hop_is_checked_including_ones_the_client_generates(monkeypatch):
    """A redirect to another host opens another connection, so it is checked too.

    The first hop passing says nothing about where the chain ends up. Driven
    through the real pool: only the socket layer underneath is canned, so the
    backend's own resolve-and-check runs for both hops.
    """
    import httpcore

    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {
        "cdn.example.com": ["93.184.216.34"],
        "second.example": ["10.0.0.7"],
    })

    connected = []

    async def fake_connect(self, host, port, timeout=None, local_address=None,
                           socket_options=None):
        connected.append(host)
        return _CannedStream(
            b"HTTP/1.1 302 Found\r\n"
            b"Location: http://second.example/r.pdf\r\n"
            b"Content-Length: 0\r\n"
            b"\r\n"
        )

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", fake_connect)

    with pytest.raises(ValueError, match="publicly routable"):
        _resolve({"file_url": "https://cdn.example.com/r.pdf"})

    # The first hop connected; the redirect target never got a socket.
    assert connected == ["93.184.216.34"]


def test_socket_opens_against_the_address_that_was_checked(monkeypatch):
    """A name is only as stable as the answer behind it — so the lookup and the
    connection happen in one place, and the socket goes where the check looked."""
    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {"files.example.com": ["93.184.216.34"]})
    opened = []
    _stub_socket_backend(monkeypatch, lambda host, port: opened.append((host, port)))

    backend = worker_net.CheckedAddressBackend("file_url")
    asyncio.run(backend.connect_tcp("files.example.com", 443))
    assert opened == [("93.184.216.34", 443)]


def test_socket_is_not_opened_when_the_address_is_rejected(monkeypatch):
    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {"internal.example": ["10.0.0.7"]})
    opened = []
    _stub_socket_backend(monkeypatch, lambda host, port: opened.append((host, port)))

    backend = worker_net.CheckedAddressBackend("file_url")
    with pytest.raises(ValueError, match="publicly routable"):
        asyncio.run(backend.connect_tcp("internal.example", 443))
    assert opened == []


def test_all_checked_addresses_are_tried_before_giving_up(monkeypatch):
    """A host with several records expects a client to work down the list.

    Using the first answer alone would strand a dual-stack name whose leading
    record this worker cannot reach — an AAAA record on a container with no
    IPv6 route being the everyday case.
    """
    import httpcore

    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {
        "dual.example.com": ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"],
    })
    attempts = []

    def on_connect(host, port):
        attempts.append(host)
        if ":" in host:
            raise httpcore.ConnectError("no route to host")
        return "stream"

    _stub_socket_backend(monkeypatch, on_connect)

    backend = worker_net.CheckedAddressBackend("file_url")
    assert asyncio.run(backend.connect_tcp("dual.example.com", 443)) == "stream"
    assert attempts == ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"]


def test_the_lookup_shares_the_budget_with_the_connect_attempts(monkeypatch):
    """What the lookup spends is taken off what the connects get."""
    import httpcore

    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)

    def slow_resolver(host, port, *args, **kwargs):
        time.sleep(0.10)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_resolver)

    handed = []

    async def record(self, host, port, timeout=None, local_address=None,
                    socket_options=None):
        handed.append(timeout)
        return "stream"

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", record)

    backend = worker_net.CheckedAddressBackend("file_url")
    asyncio.run(backend.connect_tcp("slow.example", 443, timeout=0.50))
    assert handed and handed[0] < 0.45, (
        f"connect got {handed[0]!r} of a 0.50s budget after a 0.10s lookup"
    )


def test_a_literal_address_is_judged_without_a_lookup(monkeypatch):
    """An address written as one needs no resolution to judge, so it is judged
    even on a request that would be proxied."""
    import httpx

    _clear_proxy_env(monkeypatch)
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: pytest.fail("the hook resolved a literal address"),
    )
    with pytest.raises(ValueError, match="publicly routable"):
        asyncio.run(worker_net.request_hook(
            httpx.Request("GET", "http://10.0.0.7/a.pdf")
        ))
    # A routable literal passes, still without resolving.
    asyncio.run(worker_net.request_hook(
        httpx.Request("GET", "http://93.184.216.34/a.pdf")
    ))


def test_the_request_hook_does_not_resolve(monkeypatch):
    """The hook is shape-only, so a hop costs one lookup rather than two.

    A second lookup here would sit outside the connect budget, which is what
    put resolution beyond the documented timeout in the first place.
    """
    import httpx

    calls = []
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: calls.append(a) or [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    asyncio.run(worker_net.request_hook(
        httpx.Request("GET", "https://cdn.example.com/r.pdf")
    ))
    assert calls == [], "the hook resolved the host"


def test_the_last_address_may_use_what_is_left(monkeypatch):
    """Reserving time for later answers must not shortchange the final one."""
    import httpcore

    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {"two.example": ["93.184.216.34", "93.184.216.35"]})
    handed = []

    async def first_fails_fast(self, host, port, timeout=None, local_address=None,
                               socket_options=None):
        handed.append(timeout)
        if host == "93.184.216.34":
            raise httpcore.ConnectError("connection refused")
        return "stream"

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", first_fails_fast)

    backend = worker_net.CheckedAddressBackend("file_url")
    assert asyncio.run(backend.connect_tcp("two.example", 443, timeout=1.0)) == "stream"
    # First got a share; the second, once the first returned unspent time, got
    # substantially more than that share.
    assert handed[0] < 0.60
    assert handed[1] > handed[0]


def test_attempts_stop_once_the_budget_is_spent(monkeypatch):
    import httpcore

    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {
        "many.example": ["93.184.216.34", "93.184.216.35", "93.184.216.36"],
    })

    attempts = []

    async def stall(self, host, port, timeout=None, local_address=None,
                    socket_options=None):
        attempts.append(host)
        await asyncio.sleep(0.08)
        raise httpcore.ConnectTimeout("timed out")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", stall)

    backend = worker_net.CheckedAddressBackend("file_url")
    with pytest.raises(httpcore.ConnectTimeout):
        asyncio.run(backend.connect_tcp("many.example", 443, timeout=0.10))
    assert len(attempts) < 3, (
        f"kept trying after the budget was spent: {attempts}"
    )


def test_no_timeout_means_every_address_is_still_tried(monkeypatch):
    """timeout=None is unbounded, as it is for the base backend."""
    import httpcore

    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {"two.example": ["93.184.216.34", "93.184.216.35"]})
    handed = []

    async def fail_then_pass(self, host, port, timeout=None, local_address=None,
                             socket_options=None):
        handed.append(timeout)
        if len(handed) == 1:
            raise httpcore.ConnectError("no route")
        return "stream"

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", fail_then_pass)

    backend = worker_net.CheckedAddressBackend("file_url")
    assert asyncio.run(backend.connect_tcp("two.example", 443, timeout=None)) == "stream"
    assert handed == [None, None]


def test_a_connect_failure_on_every_address_surfaces(monkeypatch):
    import httpcore

    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {"down.example.com": ["93.184.216.34", "93.184.216.35"]})

    def on_connect(host, port):
        raise httpcore.ConnectError("no route to host")

    _stub_socket_backend(monkeypatch, on_connect)

    backend = worker_net.CheckedAddressBackend("file_url")
    with pytest.raises(httpcore.ConnectError):
        asyncio.run(backend.connect_tcp("down.example.com", 443))


def test_httpx_internals_this_transport_depends_on(monkeypatch):
    """Guard: the two names CheckedTargetTransport reaches into.

    httpx exposes no way to supply a network backend, so the pool's backend is
    swapped after construction. If an upgrade moves either name, fail here
    rather than silently connecting without the check.
    """
    import httpcore
    import httpx

    plain = httpx.AsyncHTTPTransport()
    try:
        assert hasattr(plain, "_pool"), "httpx.AsyncHTTPTransport._pool is gone"
        assert hasattr(plain._pool, "_network_backend"), (
            "httpcore pool no longer holds _network_backend"
        )
    finally:
        asyncio.run(plain.aclose())

    import inspect

    signature = inspect.signature(httpcore.AnyIOBackend.connect_tcp)
    params = list(signature.parameters)
    assert params == [
        "self", "host", "port", "timeout", "local_address", "socket_options",
    ], f"AnyIOBackend.connect_tcp signature changed: {params}"

    # The override hands `host` a str, which is what socket.getaddrinfo returns
    # and what this backend takes. Passing bytes instead makes anyio treat them
    # as a name to resolve, so the connection fails. Pin the parameter type: if
    # a future version takes bytes, that has to fail here rather than on a
    # caller's first real fetch.
    host_annotation = signature.parameters["host"].annotation
    assert host_annotation in (str, "str"), (
        f"AnyIOBackend.connect_tcp host parameter is now {host_annotation!r}; "
        f"CheckedAddressBackend passes a str"
    )
