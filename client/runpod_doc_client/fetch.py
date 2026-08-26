"""Fetching an archive over HTTP, bounded in every dimension that can run away.

Three separate bounds, because each covers a case the others do not: a socket
timeout for an idle peer, a wall-clock deadline for one that trickles, and a byte
cap for one that is neither slow nor idle but endless.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import threading
import urllib.error
import urllib.request
from urllib.parse import unquote, urlsplit

from runpod_doc_client import limits
from runpod_doc_client.errors import ResponseError, _log


def require_fetchable_url(url: str) -> None:
    """Reject a URL that is not a usable HTTP(S) target, before fetching it.

    Raises on refusal and returns nothing. Deliberately *not* named
    ``require_http_url``: :func:`runpod_doc_worker.transport.net.require_http_url`
    has that name and returns the validated **host**, so two functions one import
    apart would have had the same name and different contracts, and a consumer
    writing the familiar ``url = require_http_url(url)`` would have replaced the
    URL with ``None``. That is the trap AGENTS.md already records about the
    worker-side helper; giving it a same-named sibling would have doubled it.

    Worker presigned URLs are always HTTPS. Anything else in that field means the
    result did not come from where the caller thinks it did, and ``urlopen``
    would happily read ``file://``.

    The scheme prefix alone is not enough. A string can start with ``https://`` and
    still be malformed in ways that raise from inside the stdlib rather than from
    here — ``https://[bad`` raises ``ValueError: Invalid IPv6 URL`` while splitting,
    and ``https://host:bad/x`` raises ``http.client.InvalidURL: nonnumeric port`` at
    connect time. Both escaped the single-error contract, so the parse happens here
    where it can be reported as one.
    """
    if not isinstance(url, str):
        # Iterating the characters below is the first thing that touches the
        # value, and `for character in None` raises a bare TypeError from inside
        # a function whose whole purpose is to report bad input as ResponseError.
        raise ResponseError(f"a URL should be a string; got {type(url).__name__}")
    # A request target is ASCII, and only its printable range. Everything outside
    # that reaches the network layer as a raw exception rather than as this
    # function's error: a space or control character raises `InvalidURL` from
    # inside http.client, and a non-ASCII character such as the `é` in
    # `https://example.com/é` raises `UnicodeEncodeError` while the request line
    # is being encoded. A caller that means to fetch such a path percent-encodes
    # it, which is ASCII; an IDN host needs punycode, which is also ASCII.
    #
    # The newline is the one that matters beyond tidy error types: it is how a
    # response would try to smuggle a second request line into the connection.
    for character in url:
        if not ("\x21" <= character <= "\x7e"):
            raise ResponseError(
                f"refusing to fetch {url!r}: {character!r} cannot appear in a "
                f"request target (expected printable ASCII, percent-encoded)"
            )
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise ResponseError(f"refusing to fetch {url!r}: {e}") from e
    if parts.scheme.lower() not in ("http", "https"):
        raise ResponseError(f"refusing to fetch {url!r}: expected an http(s) URL")
    try:
        host = parts.hostname
        parts.port  # noqa: B018 — property raises on a non-numeric port
    except ValueError as e:
        raise ResponseError(f"refusing to fetch {url!r}: {e}") from e
    if not host:
        raise ResponseError(f"refusing to fetch {url!r}: no host")
    # The printable-ASCII check above sees the *encoded* URL, so `http://%FF/`
    # passes it and passes the host check — and then urllib percent-decodes the
    # authority, gets U+FFFD, and raises UnicodeEncodeError building the latin-1
    # Host header. A real IDN host arrives already punycoded (`xn--…`), which is
    # ASCII, so nothing legitimate decodes to non-ASCII here.
    # The whole authority, not just the host: userinfo is percent-decoded for the
    # Authorization header the same way the host is for the Host header, so
    # `http://%FF@example.com/` passed a hostname-only check and still raised
    # UnicodeEncodeError from inside urlopen. Checking one component of a string
    # that gets decoded in several places is a check in the wrong place.
    try:
        decoded = unquote(parts.netloc, errors="strict")
        decoded.encode("ascii")
    except (UnicodeDecodeError, UnicodeEncodeError) as e:
        raise ResponseError(
            f"refusing to fetch {url!r}: the authority is not ASCII once "
            f"percent-decoded"
        ) from e
    # Printable, not merely encodable. `encode("ascii")` accepts CR and LF, so
    # `http://user%0d%0aX:y@example.com/` passed here -- the leading check sees
    # only the `%` escapes, and this one asked the wrong question about the
    # decoded result. urllib then refuses it while building the header, which
    # makes a public "is this fetchable" helper answer yes to something that is
    # not, and defeats every caller that checks before fetching.
    if any(character < " " or character == "\x7f" for character in decoded):
        raise ResponseError(
            f"refusing to fetch {url!r}: the authority decodes to a control "
            f"character"
        )


class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Applies :func:`require_fetchable_url` to every hop.

    urllib's default handler follows a ``Location`` wherever it points, and its
    opener has an FTP handler installed — so an accepted HTTPS endpoint
    redirecting to ``ftp://`` was fetched over FTP, past a validator whose whole
    documented job is to reject exactly that. Validating the URL a caller hands
    in says nothing about the ones the *server* chooses, and a redirect target is
    as untrusted as a response body.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        require_fetchable_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _refuse_unroutable_origin(host: str, url: str) -> None:
    """Refuse a *requested* origin that resolves anywhere non-routable.

    Used when a proxy carries the request. The socket then connects to the proxy,
    so the peer address answers a question nobody asked: a public proxy asked to
    fetch `http://169.254.169.254/` was approved because the *proxy* is public,
    and a private corporate proxy was refused for every public target because the
    proxy is private. Both wrong, in opposite directions.

    Every address the name resolves to has to pass, not merely the first: a name
    answering with one public and one private address would otherwise depend on
    resolver ordering.

    This is weaker than the direct check and unavoidably so. With a proxy the
    client never sees the address the request actually reaches, so a name that
    resolves differently for the proxy defeats this -- the proxy is the trust
    boundary at that point, which is what choosing to route through one means.
    """
    try:
        resolved = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ResponseError(
            f"refusing to fetch {url!r}: {host} could not be resolved: {e}"
        ) from e
    for entry in resolved:
        address = ipaddress.ip_address(entry[4][0])
        if not address.is_global:
            raise ResponseError(
                f"refusing to fetch {url!r}: {host} resolves to {address}, which "
                f"is not a routable public address. Set "
                f"runpod_doc_client.limits.ALLOW_PRIVATE_FETCH_TARGETS = "
                f"True if this worker really does serve from a private network."
            )


def _refuse_unroutable(sock: socket.socket | None, url: str) -> None:
    """Refuse a connection that landed on an address a client should not reach.

    Read off the connected socket rather than resolved from the hostname. A name
    can answer with a public address when it is checked and a private one when it
    is dialled -- classic DNS rebinding -- so the only address worth judging is
    the one the connection actually reached. `getpeername()` is that address.

    `is_global` covers loopback, link-local (169.254.0.0/16, where the cloud
    metadata service lives), the private ranges, multicast and the reserved
    blocks in one predicate, and it is in the standard library, which this
    subpackage is restricted to.
    """
    if limits.ALLOW_PRIVATE_FETCH_TARGETS or sock is None:
        return
    try:
        peer = sock.getpeername()
    except OSError:  # pragma: no cover - already gone
        return
    if not isinstance(peer, tuple) or not peer:
        return
    try:
        address = ipaddress.ip_address(peer[0])
    except ValueError:  # pragma: no cover - a unix socket, not our concern
        return
    if address.is_global:
        return
    raise ResponseError(
        f"refusing to fetch {url!r}: it connects to {address}, which is not a "
        f"routable public address. Set "
        f"runpod_doc_client.limits.ALLOW_PRIVATE_FETCH_TARGETS = True if "
        f"this worker really does serve from a private network."
    )


class _ConnectionRecorder:
    """Publishes each connection into a caller-supplied dict as it is created.

    The deadline needs an object to close, and the response does not exist until
    ``open()`` returns -- so a server that trickles *headers* left the timeout
    with nothing to cancel and the fetch went on reading. The connection is
    created well before the headers are parsed, and closing it makes the blocked
    read fail at once.

    ``do_open`` is the interception point rather than ``http_open``/``https_open``
    because it is where the connection class is used, and it receives whatever
    keyword arguments this interpreter's handler passes (``context``,
    ``check_hostname``) without this code having to know them.
    """

    def __init__(self, sink: dict[str, object]) -> None:
        super().__init__()
        self._sink = sink

    def do_open(self, http_class, req, **kwargs):
        def build(host, **connection_args):
            connection = http_class(host, **connection_args)
            self._sink["connection"] = connection
            # Wrap `connect` so the address check runs for this hop and every
            # redirect, since each one builds its own connection through here.
            # Checking in `download` would cover the first hop only.
            original_connect = getattr(connection, "connect", None)
            if callable(original_connect):

                def checked_connect() -> None:
                    # Which address to judge depends on who the socket reaches.
                    # Through a proxy it reaches the proxy, so the peer address
                    # says nothing about the target -- the requested origin has to
                    # be judged instead, before the request is sent.
                    tunnelled = getattr(connection, "_tunnel_host", None)
                    proxied = tunnelled is not None or req.has_proxy()
                    if proxied and not limits.ALLOW_PRIVATE_FETCH_TARGETS:
                        target = tunnelled or urlsplit(req.full_url).hostname
                        if target:
                            # Passed through, not split. `urlsplit().hostname`
                            # has already dropped any port and the brackets from
                            # an IPv6 literal, so `2606:4700::1111` was being cut
                            # to `2606` -- which `getaddrinfo` reads as the IPv4
                            # address 0.0.10.46 and this refuses. Every public
                            # IPv6 download failed whenever a proxy was set.
                            _refuse_unroutable_origin(target, req.full_url)
                    original_connect()
                    if not proxied:
                        _refuse_unroutable(
                            getattr(connection, "sock", None), req.full_url
                        )

                connection.connect = checked_connect
            return connection

        return super().do_open(build, req, **kwargs)


class _RecordingHTTPHandler(_ConnectionRecorder, urllib.request.HTTPHandler):
    """``HTTPHandler``, publishing its connection as it is created."""


class _RecordingHTTPSHandler(_ConnectionRecorder, urllib.request.HTTPSHandler):
    """``HTTPSHandler``, publishing its connection as it is created."""


def _opener(sink: dict[str, object] | None = None) -> urllib.request.OpenerDirector:
    """An opener that speaks only HTTP(S) and checks every redirect.

    Assembled from an empty ``OpenerDirector`` rather than with
    ``build_opener``, which *adds to* the default handler set rather than
    replacing it — so passing the HTTP handlers still left ``FTPHandler`` and
    ``FileHandler`` installed, which is what a first attempt at this did. The
    redirect check above already refuses a non-HTTP hop; removing the handlers
    means a miss there has nothing to reach.

    ``ProxyHandler`` is kept deliberately: an operator behind a proxy expects
    ``HTTPS_PROXY`` to be honoured, and dropping it would silently change how
    every fetch is routed.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        _RecordingHTTPHandler(sink)
        if sink is not None
        else urllib.request.HTTPHandler(),
        _RecordingHTTPSHandler(sink)
        if sink is not None
        else urllib.request.HTTPSHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.UnknownHandler(),
        _CheckedRedirectHandler(),
    ):
        opener.add_handler(handler)
    return opener


def _fetch(url: str, holder: dict[str, object]) -> bytes:
    """Open, read and return the body. Blocking; bounded by its caller."""
    with _opener(holder).open(  # noqa: S310 - scheme checked, redirects checked
        url, timeout=limits.DOWNLOAD_TIMEOUT_SECONDS
    ) as response:
        # Published so the caller can close it on timeout. A daemon thread only
        # stops holding the *process* open; it goes on holding a socket and its
        # accumulated chunks, so repeated timed-out fetches would each retain one.
        holder["response"] = response
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > limits.MAX_ARCHIVE_BYTES:
            raise ResponseError(
                f"the archive is {int(declared)} bytes, over the "
                f"{limits.MAX_ARCHIVE_BYTES}-byte limit"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limits.MAX_ARCHIVE_BYTES:
                raise ResponseError(
                    f"the archive exceeds the {limits.MAX_ARCHIVE_BYTES}-byte limit"
                )
            chunks.append(chunk)
        # Reading in chunks loses the truncation check `read()` performs for free:
        # `read(n)` returns what has arrived and then b"" at EOF, so a server that
        # hangs up early yields a short body rather than IncompleteRead.
        if declared and declared.isdigit() and total < int(declared):
            raise ResponseError(
                f"fetching the archive failed: IncompleteRead "
                f"({total} bytes read, {int(declared) - total} more expected)"
            )
        return b"".join(chunks)


def download(url: str) -> bytes:
    """Fetch an archive. Network failures arrive as :class:`ResponseError`.

    An expired presigned URL, an endpoint that is refusing, or a stalled read all
    used to surface as urllib exceptions straight past a client's own handler.
    The ordinary case is the expired URL, which is also the one a caller most
    needs to catch.

    The fetch runs on a worker thread joined against a deadline. Two earlier
    attempts checked a clock in the read loop, and neither bounded anything: the
    timeout urllib takes is an *idle* socket timeout, so a server trickling header
    bytes can keep ``open()`` inside the network stack indefinitely, and a
    trickled chunk does the same to a single ``read()``. A clock consulted after a
    blocking call returns cannot bound that call -- the only way to bound it from
    here is to stop waiting on it.

    On timeout the response is closed, which makes the blocked read fail and
    releases the socket immediately. Marking the thread a daemon was not enough on
    its own: that only stops an abandoned fetch holding the *process* open, while
    it goes on holding a connection and its accumulated chunks.
    """
    require_fetchable_url(url)
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["body"] = _fetch(url, outcome)
        except BaseException as error:  # noqa: BLE001 - re-raised on the caller
            outcome["error"] = error

    worker = threading.Thread(target=run, name="runpod-doc-worker-fetch", daemon=True)
    worker.start()
    worker.join(limits.DOWNLOAD_DEADLINE_SECONDS)
    if worker.is_alive():
        # Close the response so the blocked read fails and the socket is released
        # now rather than whenever the idle timeout happens to fire. Without this
        # the deadline bounded only the *caller*: the fetch carried on reading, and
        # a series of timed-out downloads accumulated a thread, a connection and a
        # growing chunk list apiece.
        # Shut the socket down first, then close. `close()` on either object is
        # not enough on its own: `HTTPResponse` holds a file object made from the
        # same socket, and a socket with outstanding `makefile` references does
        # not release its descriptor when closed -- so the connection survived.
        # Measured on Linux against a trickling local server: after `close()`
        # alone the server went on writing headers successfully, and after
        # `shutdown` its next write failed. `shutdown` acts on the descriptor
        # rather than on a reference to it, which is the whole difference.
        connection = outcome.get("connection")
        sock = getattr(connection, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                # Already gone, or never connected. Either way there is nothing
                # left to cancel.
                pass
        # Then both objects, response first: it owns the file wrapper, and the
        # response only exists once the headers have been read, so a timeout in
        # the header phase has only the connection.
        for key in ("response", "connection"):
            target = outcome.get(key)
            close = getattr(target, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - already abandoning this fetch
                    pass
        # Brief, and deliberately not longer. After a shutdown the blocked read
        # raises at once, so a second is generous -- and waiting any longer would
        # turn "the deadline bounds the caller" into "the deadline plus the wait",
        # which is the guarantee this whole path exists to provide. A first
        # attempt at this used five seconds and broke exactly that.
        worker.join(1.0)
        if worker.is_alive() and sock is not None:
            # Only when there was a socket to shut down. Without one there was
            # nothing to cancel and no leak to report -- and reporting one anyway
            # would cry wolf on every caller that never reached the network.
            # Reported rather than ignored otherwise: a thread still running here
            # means the cancellation did not work, and this has previously been
            # believed working while it was not.
            _log.warning(
                "the timed-out fetch did not stop after its socket was shut down"
            )
        raise ResponseError(
            f"fetching the archive exceeded {limits.DOWNLOAD_DEADLINE_SECONDS:.0f}s"
        )

    error = outcome.get("error")
    if error is not None:
        if isinstance(error, ResponseError):
            raise error
        if isinstance(error, urllib.error.HTTPError):
            raise ResponseError(
                f"fetching the archive failed: HTTP {error.code}"
            ) from error
        if isinstance(error, urllib.error.URLError):
            raise ResponseError(
                f"fetching the archive failed: {error.reason}"
            ) from error
        if isinstance(error, http.client.HTTPException):
            raise ResponseError(
                f"fetching the archive failed: {type(error).__name__}: {error}"
            ) from error
        if isinstance(error, (TimeoutError, OSError, ValueError, UnicodeError)):
            raise ResponseError(
                f"fetching the archive failed: {type(error).__name__}: {error}"
            ) from error
        raise error
    body = outcome.get("body")
    if not isinstance(body, bytes):  # pragma: no cover - defensive
        raise ResponseError("fetching the archive produced no body")
    return body
