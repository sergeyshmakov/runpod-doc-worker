"""Input-source resolution: which files and which URLs the worker accepts. -- network. -- deadline."""

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


_PROXY_VARS = (
    "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
)


def test_a_stalled_lookup_is_bounded_by_the_fetch_budget(monkeypatch):
    """Resolution is inside the budget, not ahead of it.

    A lookup runs before any connection and can block for as long as the
    platform resolver allows, so leaving it outside would let a name that never
    answers hold a fetch open past the timeout the caller was given.
    """
    import httpcore

    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)

    def never_answers(host, port, *args, **kwargs):
        time.sleep(5)  # far past the budget below
        raise AssertionError("resolution should have been abandoned")

    monkeypatch.setattr(socket, "getaddrinfo", never_answers)

    backend = worker_net.CheckedAddressBackend("file_url")

    async def measure():
        # Timed inside the loop on purpose: the abandoned lookup keeps running
        # in its thread (getaddrinfo cannot be interrupted), and asyncio.run
        # joins the executor on the way out. What has to be bounded is the time
        # the job waits, which is what this measures.
        started = time.monotonic()
        with pytest.raises(httpcore.ConnectTimeout, match="outlasted the fetch budget"):
            await backend.connect_tcp("stalls.example", 443, timeout=0.15)
        return time.monotonic() - started

    elapsed = asyncio.run(measure())
    assert elapsed < 1.0, f"job waited {elapsed:.2f}s on a 0.15s budget"


def test_the_connect_timeout_is_one_budget_for_the_whole_address_list(monkeypatch):
    """Walking the address list must not multiply the caller's timeout.

    The base backend bounds a connect to a name with a single deadline covering
    every address it tries. Handing the full timeout to each attempt instead
    would let a name with several records that accept and then say nothing hold
    the fetch for len(addresses) times as long as promised.
    """
    import httpcore

    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {
        "many.example": ["93.184.216.34", "93.184.216.35", "93.184.216.36"],
    })

    handed = []

    async def stall(self, host, port, timeout=None, local_address=None,
                    socket_options=None):
        handed.append(timeout)
        await asyncio.sleep(0.05)
        raise httpcore.ConnectTimeout("timed out")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", stall)

    backend = worker_net.CheckedAddressBackend("file_url")
    started = time.monotonic()
    with pytest.raises(httpcore.ConnectTimeout):
        asyncio.run(backend.connect_tcp("many.example", 443, timeout=0.12))
    elapsed = time.monotonic() - started

    assert handed, "no attempt was made"
    # Each attempt gets what is left, never the original budget again.
    assert handed[0] <= 0.12
    assert all(
        later < earlier for earlier, later in zip(handed, handed[1:])
    ), f"timeout was not shared across attempts: {handed}"
    assert elapsed < 0.12 * len(handed), (
        f"took {elapsed:.3f}s across {len(handed)} attempts on a 0.12s budget"
    )


def test_a_stalling_address_does_not_strand_the_reachable_ones(monkeypatch):
    """An address that accepts and then says nothing must not spend the budget.

    Failing fast already fell through to the next answer; burning the whole
    deadline did not, which stranded exactly the dual-stack case this walk
    exists for.
    """
    import httpcore

    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {
        "dual.example": ["93.184.216.34", "93.184.216.35"],
    })
    attempts = []

    async def first_stalls(self, host, port, timeout=None, local_address=None,
                           socket_options=None):
        attempts.append(host)
        if host == "93.184.216.34":
            await asyncio.sleep(timeout)      # silently drops for its whole slice
            raise httpcore.ConnectTimeout("timed out")
        return "stream"

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", first_stalls)

    backend = worker_net.CheckedAddressBackend("file_url")

    async def measure():
        started = time.monotonic()
        stream = await backend.connect_tcp("dual.example", 443, timeout=0.40)
        return stream, time.monotonic() - started

    stream, elapsed = asyncio.run(measure())
    assert stream == "stream", "never reached the reachable address"
    assert attempts == ["93.184.216.34", "93.184.216.35"]
    assert elapsed < 0.40, f"used {elapsed:.2f}s of a 0.40s budget"


def test_a_slow_drip_download_hits_the_wall_clock_budget(monkeypatch):
    """httpx's timeout resets on every byte, so a server sending one chunk just
    inside it never trips the timeout and never approaches the size cap — the
    download is bounded by nothing without a total budget."""
    class _Drip:
        def __init__(self):
            self.headers = {}
            self.status_code = 200

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            # Each chunk advances the fake clock well inside the inactivity
            # timeout, so only the wall-clock budget can stop this.
            for _ in range(10_000):
                clock.advance(60.0)
                yield b"x"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Clock:
        def __init__(self):
            self.now = 1000.0

        def advance(self, seconds):
            self.now += seconds

        def __call__(self):
            return self.now

    clock = _Clock()
    monkeypatch.setattr(worker_io.time, "monotonic", clock)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *a, **k):
            return _Drip()

    monkeypatch.setattr(worker_io.httpx, "AsyncClient", _Client)

    with pytest.raises(ValueError, match="exceeded the .* budget"):
        _resolve({"file_url": "https://cdn.example.com/slow.pdf"})


def test_a_stalled_connect_is_bounded_even_though_no_chunk_arrives(monkeypatch):
    """The in-loop deadline can only fire once a chunk has arrived. A chain of
    slow redirects, or a connection that never produces a first byte, needs the
    budget applied around the whole fetch."""
    async def never_returns(_file_url):
        await asyncio.sleep(3600)

    monkeypatch.setattr(worker_io, "_fetch_url", never_returns)
    monkeypatch.setattr(worker_io, "MAX_URL_FETCH_SECONDS", 0.05)

    with pytest.raises(ValueError, match="exceeded the .*budget"):
        _resolve({"file_url": "https://cdn.example.com/stalls.pdf"})
