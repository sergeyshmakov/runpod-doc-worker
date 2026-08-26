"""Downloading an archive: URL checks, redirects, and the three bounds."""

from __future__ import annotations

import http.server
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request

import pytest

from runpod_doc_client import (
    ResponseError,
    download,
    fetch,
    limits,
)


@pytest.mark.parametrize(
    "raised",
    [
        TimeoutError("stalled mid-body"),
        urllib.error.URLError("dns failure"),
        urllib.error.HTTPError("https://x/a.tar", 403, "Forbidden", {}, None),
    ],
)
def test_a_fetch_failure_is_refused(monkeypatch: pytest.MonkeyPatch, raised) -> None:
    """An expired presigned URL is the ordinary case here, not an exotic one, and
    a bare TimeoutError arrives unwrapped when the stall is in the body."""
    # `_opener`, not `urllib.request.urlopen`: download() stopped using the
    # module-level function when it gained a redirect-checking opener, and this
    # patch silently stopped applying. The test then passed on whatever a real
    # request to example.com did — a network error satisfied the assertion while
    # exercising none of these exceptions, and a success or a slow response would
    # have failed or hung the suite instead.
    class _Exploding:
        def open(self, *args: object, **kwargs: object):
            raise raised

    monkeypatch.setattr(fetch, "_opener", lambda *_: _Exploding())
    with pytest.raises(ResponseError, match="fetching the archive failed"):
        download("https://example.com/out.tar.gz")


class _TruncatingHandler(http.server.BaseHTTPRequestHandler):
    """Declares 4096 bytes, sends 512, hangs up."""

    def do_GET(self) -> None:  # noqa: N802 — stdlib's spelling
        self.send_response(200)
        self.send_header("Content-Length", "4096")
        self.end_headers()
        self.wfile.write(b"x" * 512)
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


def test_a_truncated_download_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server closing before its declared Content-Length raises
    `http.client.IncompleteRead` from `read()`. It is an HTTPException rather
    than an OSError, so the interrupted-download case — the most ordinary
    network failure there is — escaped every handler in `download`."""
    # Loopback, which the routability guard refuses by default. This is the
    # case `ALLOW_PRIVATE_FETCH_TARGETS` exists for, so the test sets it rather
    # than working around it.
    monkeypatch.setattr(limits, "ALLOW_PRIVATE_FETCH_TARGETS", True)
    server = socketserver.TCPServer(("127.0.0.1", 0), _TruncatingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(ResponseError, match="IncompleteRead"):
            download(f"http://127.0.0.1:{server.server_address[1]}/a.tar")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_an_oversized_download_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The socket timeout bounds idle time, not total volume, so a peer that keeps
    sending holds the connection and grows the buffer without ever tripping it."""
    monkeypatch.setattr(limits, "MAX_ARCHIVE_BYTES", 4096)

    class _Endless:
        headers: dict[str, str] = {}

        def read(self, size: int = -1) -> bytes:
            return b"x" * (size if size and size > 0 else 1024)

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(fetch, "_opener", lambda *_: type("O", (), {"open": lambda *a, **k: _Endless()})())
    with pytest.raises(ResponseError, match="limit"):
        download("https://example.com/endless.tar")


def test_a_declared_oversize_is_refused_before_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """An early exit on Content-Length, which is a courtesy rather than the
    protection - the running total is what actually stops an untruthful one."""
    monkeypatch.setattr(limits, "MAX_ARCHIVE_BYTES", 4096)

    class _Declared:
        headers = {"Content-Length": "999999999"}

        def read(self, size: int = -1) -> bytes:
            raise AssertionError("must not read a body it already refused")

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(fetch, "_opener", lambda *_: type("O", (), {"open": lambda *a, **k: _Declared()})())
    with pytest.raises(ResponseError, match="over the"):
        download("https://example.com/huge.tar")


def test_a_download_that_never_finishes_hits_the_deadline(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket timeout bounds idle time and is reset by every successful read,
    so a peer trickling bytes can hold the call open indefinitely without ever
    approaching the byte cap."""
    monkeypatch.setattr(limits, "DOWNLOAD_DEADLINE_SECONDS", 0.05)

    class _Trickle:
        headers: dict[str, str] = {}

        def read(self, size: int = -1) -> bytes:
            time.sleep(0.02)
            return b"x"

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(
        fetch, "_opener", lambda *_: type("O", (), {"open": lambda *a, **k: _Trickle()})()
    )
    with pytest.raises(ResponseError, match="exceeded"):
        download("https://example.com/trickle.tar")


def test_the_deadline_covers_connection_and_headers(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timer started after `open()` returned, so connection setup, every
    redirect hop and the response headers were all outside it — and a server can
    trickle header bytes often enough that the per-socket idle timeout never
    fires. A deadline that begins after the slow part is not a deadline."""
    monkeypatch.setattr(limits, "DOWNLOAD_DEADLINE_SECONDS", 0.01)

    class _SlowOpen:
        def open(self, *args: object, **kwargs: object):
            time.sleep(0.05)

            class _Empty:
                headers: dict[str, str] = {}

                def read(self, size: int = -1) -> bytes:
                    return b""

                def __enter__(self):
                    return self

                def __exit__(self, *exc: object) -> None:
                    return None

            return _Empty()

    monkeypatch.setattr(fetch, "_opener", lambda *_: _SlowOpen())
    with pytest.raises(ResponseError, match="exceeded"):
        download("https://example.com/slow.tar")


def test_the_deadline_bounds_a_blocking_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two earlier attempts checked a clock in the read loop, and neither bounded
    anything: the timeout urllib takes is an *idle* socket timeout, so a server
    trickling header bytes keeps `open()` inside the network stack indefinitely.

    A clock consulted after a blocking call returns cannot bound that call. The
    fetch runs on a daemon thread joined against the deadline, so the only way to
    bound it — stopping waiting on it — is what actually happens.
    """
    monkeypatch.setattr(limits, "DOWNLOAD_DEADLINE_SECONDS", 0.2)

    class _NeverReturns:
        def open(self, *args: object, **kwargs: object):
            time.sleep(30)

    monkeypatch.setattr(fetch, "_opener", lambda *_: _NeverReturns())
    started = time.monotonic()
    with pytest.raises(ResponseError, match="exceeded"):
        download("https://example.com/blocked.tar")
    assert time.monotonic() - started < 5, "the call must not wait for the sleep"


def test_a_successful_fetch_still_returns_its_body(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thread has to hand the body back, not just the failures."""

    class _Body:
        headers = {"Content-Length": "5"}

        def __init__(self) -> None:
            self._sent = False

        def read(self, size: int = -1) -> bytes:
            if self._sent:
                return b""
            self._sent = True
            return b"hello"

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(
        fetch, "_opener", lambda *_: type("O", (), {"open": lambda *a, **k: _Body()})()
    )
    assert download("https://example.com/ok.tar") == b"hello"


def test_a_timed_out_fetch_closes_its_response(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marking the thread a daemon only stops an abandoned fetch holding the
    *process* open — it goes on holding a socket and its accumulated chunks, so a
    series of timed-out downloads would retain one apiece.

    The response is closed on timeout, which makes the blocked read fail and
    releases the connection immediately.
    """
    monkeypatch.setattr(limits, "DOWNLOAD_DEADLINE_SECONDS", 0.2)
    closed: list[bool] = []

    class _Stalling:
        headers: dict[str, str] = {}

        def read(self, size: int = -1) -> bytes:
            time.sleep(5)
            return b"x"

        def close(self) -> None:
            closed.append(True)

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(
        fetch,
        "_opener",
        lambda *_: type("O", (), {"open": lambda *a, **k: _Stalling()})(),
    )
    with pytest.raises(ResponseError, match="exceeded"):
        download("https://example.com/stalled.tar")
    assert closed, "the response was abandoned rather than closed"


def test_the_deadline_cancels_a_stall_before_the_headers_arrive(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that trickles headers left the timeout with nothing to close.

    `open()` had not returned, so no response had been published -- and the
    previous fix closed only the response. The fetch went on holding the socket
    until its own idle timeout, which is the state that fix was meant to end. The
    connection exists well before the headers are parsed, so it is what gets
    closed.
    """
    closed: list[str] = []
    release = threading.Event()

    class _Connection:
        def close(self) -> None:
            closed.append("connection")
            release.set()

    class _Opener:
        def __init__(self, sink: dict[str, object]) -> None:
            self._sink = sink

        def open(self, *args: object, **kwargs: object) -> object:
            self._sink["connection"] = _Connection()
            release.wait(10)
            raise AssertionError("the stalled open should never complete")

    monkeypatch.setattr(limits, "DOWNLOAD_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(fetch, "_opener", lambda sink=None: _Opener(sink))
    try:
        with pytest.raises(ResponseError, match="exceeded"):
            download("https://example.com/trickled-headers.tar")
        assert closed == ["connection"], (
            "the header-phase stall was abandoned rather than cancelled"
        )
    finally:
        release.set()


def test_a_timed_out_fetch_really_releases_its_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Against a real socket, and asserting a number that a stub cannot produce.

    A fake opener can only answer the question the fix asks it, and this failure
    is one no stub can show: closing the `HTTPConnection` does not release the
    descriptor, because `HTTPResponse` holds a file object made from the same
    socket and a socket with outstanding `makefile` references keeps its descriptor
    on close. So the connection survived and the server kept writing.

    Asking whether the server's writes eventually fail is not enough either -- they
    fail either way, just later. What separates the two is how much the server gets
    to write after the deadline has already fired. Measured on Linux through this
    exact path:

        shutdown        1 write after the deadline, download returned in 0.40s
        no shutdown    32 writes after the deadline, download returned in 1.40s

    So the assertion is on that count, with room for the one write already in
    flight. Windows tears the connection down on close by itself, which is why the
    same code passes on Windows regardless, so this is checked in the Linux
    matrix.
    """
    # Loopback, which the routability guard refuses by default. This is the
    # case `ALLOW_PRIVATE_FETCH_TARGETS` exists for, so the test sets it rather
    # than working around it.
    monkeypatch.setattr(limits, "ALLOW_PRIVATE_FETCH_TARGETS", True)
    monkeypatch.setattr(limits, "DOWNLOAD_DEADLINE_SECONDS", 0.4)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    fired = threading.Event()
    finished = threading.Event()
    after_deadline = 0
    served = threading.Event()

    def serve() -> None:
        nonlocal after_deadline
        connection, _ = listener.accept()
        served.set()
        try:
            connection.recv(4096)
            connection.sendall(b"HTTP/1.1 200 OK\r\n")
            for _ in range(400):
                connection.sendall(b"X-Trickle: 1\r\n")
                if fired.is_set():
                    after_deadline += 1
                time.sleep(0.02)
        except OSError:
            pass
        finally:
            finished.set()
            connection.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        with pytest.raises(ResponseError, match="exceeded"):
            download(f"http://127.0.0.1:{port}/trickle.tar")
        fired.set()
        assert served.is_set(), "the fixture never accepted a connection"
        assert finished.wait(5.0), "the server loop never ended"
        assert after_deadline <= 3, (
            f"the server completed {after_deadline} writes after the deadline; the "
            f"connection was closed by reference but its descriptor was never shut "
            f"down, so the fetch went on reading"
        )
    finally:
        listener.close()
