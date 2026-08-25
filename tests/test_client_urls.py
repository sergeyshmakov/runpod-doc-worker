"""The URL a response may be fetched from: scheme, authority, redirects."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from runpod_doc_worker.client import (
    ResponseError,
    download,
    fetch,
    limits,
    require_fetchable_url,
)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "/local/path"])
def test_a_non_http_url_is_refused_before_fetching(url: str) -> None:
    """`urlopen` would happily read `file://`."""
    with pytest.raises(ResponseError, match="expected an http"):
        require_fetchable_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://[bad",                  # ValueError: Invalid IPv6 URL, while splitting
        "https://example.com:bad/x",     # http.client.InvalidURL, at connect time
        "https://",                      # no host at all
    ],
)
def test_a_malformed_url_that_starts_with_a_good_scheme_is_refused(url: str) -> None:
    """The prefix check accepted these, and the failure surfaced from inside the
    stdlib instead of from here."""
    with pytest.raises(ResponseError, match="refusing to fetch"):
        download(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/has space",
        "https://example.com\n.evil/x",
        "https://example.com/\ttab",
    ],
)
def test_a_url_with_a_forbidden_character_is_refused(url: str) -> None:
    """`urlopen` raises `InvalidURL` from inside http.client for these. The newline
    is the one that matters most: it is how a response would try to smuggle a
    second request line into the connection."""
    with pytest.raises(ResponseError, match="cannot appear in a request target"):
        download(url)


def test_a_non_ascii_url_is_refused() -> None:
    """A request target is ASCII. `https://example.com/é` passes a scheme check
    and then raises `UnicodeEncodeError` while the request line is encoded — a
    caller meaning to fetch that path percent-encodes it, which is ASCII."""
    with pytest.raises(ResponseError, match="cannot appear in a request target"):
        download("https://example.com/é")


@pytest.mark.parametrize("url", [None, 1234, b"https://example.com/a.tar", ["x"]])
def test_a_url_that_is_not_a_string_is_refused(url: object) -> None:
    """`for character in None` raises a bare TypeError from inside the function
    whose whole job is to report bad input as ResponseError. A parsed response
    honours its annotation only if the worker sent what it promised."""
    with pytest.raises(ResponseError, match="should be a string"):
        require_fetchable_url(url)  # type: ignore[arg-type]


def test_a_percent_encoded_non_ascii_host_is_refused() -> None:
    """`http://%FF/` passes a printable-ASCII check because the check sees the
    encoded form. urllib then percent-decodes the authority, gets U+FFFD, and
    raises UnicodeEncodeError building the latin-1 Host header. A real IDN host
    arrives punycoded, which is ASCII, so nothing legitimate is refused."""
    with pytest.raises(ResponseError, match="not ASCII once percent-decoded"):
        download("http://%FF/")


def test_the_client_url_helper_does_not_share_a_name_with_the_worker_one() -> None:
    """`transport.net.require_http_url` returns the validated **host**; this
    module's helper returns nothing and raises on refusal.

    Two functions one import apart with the same name and different contracts is
    the exact trap AGENTS.md records about the worker-side helper — a consumer
    writing `url = require_http_url(url)` gets None. Naming the client's the same
    thing would have doubled it, so it is `require_fetchable_url` and the old name
    is not exported.
    """
    from runpod_doc_worker import client
    from runpod_doc_worker.transport import net

    assert not hasattr(client, "require_http_url"), "the colliding name is exported"
    assert "require_fetchable_url" in client.__all__
    # The contracts really do differ, which is why the names must.
    assert net.require_http_url("https://example.com/a.tar", field="u") == "example.com"
    assert require_fetchable_url("https://example.com/a.tar") is None


def test_percent_encoded_userinfo_is_refused() -> None:
    """Only the hostname was decoded and checked, so `http://%FF@example.com/`
    passed and then raised UnicodeEncodeError building the Authorization header.
    Checking one component of a string that gets decoded in several places is a
    check in the wrong place."""
    with pytest.raises(ResponseError, match="not ASCII once percent-decoded"):
        download("http://%FF@example.com/")


def test_a_malformed_redirect_target_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validating the URL handed in says nothing about where the server sends us
    next. urllib follows redirects internally, so a `Location` of `http://[bad`
    raises ValueError from inside urlopen without passing through the
    validator."""
    class _Exploding:
        def open(self, *args: object, **kwargs: object):
            raise ValueError("Invalid IPv6 URL")

    monkeypatch.setattr(fetch, "_opener", lambda *_: _Exploding())
    with pytest.raises(ResponseError, match="fetching the archive failed"):
        download("https://example.com/out.tar.gz")


def test_a_redirect_to_another_scheme_is_refused() -> None:
    """urllib's default opener has an FTP handler installed and its redirect
    handler follows a `Location` wherever it points, so an accepted HTTPS
    endpoint redirecting to `ftp://` was fetched over FTP — past a validator
    whose documented job is to reject exactly that.

    Asserted against the redirect handler directly rather than by standing up a
    redirecting server, because the check belongs to the handler and this keeps
    the test off the network.
    """
    from runpod_doc_worker.client.fetch import _CheckedRedirectHandler

    handler = _CheckedRedirectHandler()
    with pytest.raises(ResponseError, match="expected an http"):
        handler.redirect_request(None, None, 302, "Found", {}, "ftp://example.com/x")


def test_the_opener_offers_only_http_handlers() -> None:
    """The opener is built explicitly instead of using the module default, whose
    handler set includes FTP and local file access — capabilities this function
    has no use for and cannot safely offer an untrusted URL."""
    from runpod_doc_worker.client.fetch import _opener

    names = {type(h).__name__ for h in _opener().handlers}
    assert "FTPHandler" not in names
    assert "FileHandler" not in names


def test_the_recording_handler_publishes_the_connection_it_builds(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connection is captured where urllib creates it, keyword arguments and
    all -- `do_open` rather than `http_open`, so `context` and `check_hostname`
    are forwarded without this code having to know which of them this
    interpreter's handler passes."""
    sink: dict[str, object] = {}
    handler = fetch._RecordingHTTPHandler(sink)

    class _Connection:
        def __init__(self, host: str, **kwargs: object) -> None:
            self.host = host
            self.kwargs = kwargs

    def fake_do_open(self, http_class, req, **kwargs):
        # What AbstractHTTPHandler does with the factory it is handed.
        return http_class("example.com", timeout=7, **kwargs)

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", fake_do_open)

    built = handler.do_open(_Connection, object(), context="ctx")
    assert sink["connection"] is built
    assert built.host == "example.com"
    assert built.kwargs == {"timeout": 7, "context": "ctx"}


def test_the_opener_records_connections_only_when_given_somewhere_to_put_them(
) -> None:
    """The recording handlers are opt-in, so every caller that does not need
    cancellation keeps the plain handler set -- which the handler-inventory test
    above is asserting about."""
    plain = {type(h).__name__ for h in fetch._opener().handlers}
    assert "HTTPSHandler" in plain
    assert "_RecordingHTTPSHandler" not in plain

    recording = {type(h).__name__ for h in fetch._opener({}).handlers}
    assert "_RecordingHTTPSHandler" in recording
    assert "_RecordingHTTPHandler" in recording


@pytest.mark.parametrize(
    "url",
    [
        "http://user%0d%0aX:y@example.com/a.tar",
        "http://user%00:y@example.com/a.tar",
        "https://%09host@example.com/a.tar",
    ],
)
def test_a_percent_encoded_control_character_in_userinfo_is_refused(url: str) -> None:
    """`encode("ascii")` accepts CR and LF, so asking whether the decoded authority
    *encodes* is the wrong question -- it has to be printable.

    The leading check sees only the `%` escapes and passes, and urllib then refuses
    the request while building the header. A public "is this fetchable" helper that
    answers yes to something unfetchable defeats every caller who checks first,
    which is the only reason to have the helper.
    """
    with pytest.raises(ResponseError, match="control character"):
        require_fetchable_url(url)


def test_an_ordinary_percent_encoded_userinfo_still_passes() -> None:
    """The guard: percent-encoding in userinfo is legal and common. Refusing all of
    it would trade an unfetchable-URL bug for a rejection of working ones."""
    require_fetchable_url("http://user%40name:pass%21@example.com/a.tar")


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "169.254.169.254", "10.0.0.1", "192.168.1.1", "::1"],
)
def test_a_connection_to_a_private_address_is_refused(address: str) -> None:
    """The URL comes from a worker response, so the address it reaches is the
    worker's choice.

    Loopback, the cloud metadata service at 169.254.169.254, and the private
    ranges are all reachable from a typical client, and a scheme-and-syntax check
    accepts every one of them. Judged on the connected socket rather than on the
    hostname, because a name can answer publicly when checked and privately when
    dialled -- and a pre-flight lookup cannot see that.
    """
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    port = 443 if ":" in address else 80

    class _Peer:
        def getpeername(self):  # noqa: ANN202
            return (address, port) if family == socket.AF_INET else (address, port, 0, 0)

    with pytest.raises(ResponseError, match="not a routable public address"):
        fetch._refuse_unroutable(_Peer(), f"http://{address}/a.tar")


def test_a_public_address_is_allowed() -> None:
    class _Peer:
        def getpeername(self):  # noqa: ANN202
            return ("93.184.216.34", 80)

    fetch._refuse_unroutable(_Peer(), "http://example.com/a.tar")


def test_the_operator_can_allow_a_private_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that really does serve from a private network is the case the flag
    exists for. Without it the only way past this would be not using `download`."""
    monkeypatch.setattr(limits, "ALLOW_PRIVATE_FETCH_TARGETS", True)

    class _Peer:
        def getpeername(self):  # noqa: ANN202
            return ("10.0.0.1", 80)

    fetch._refuse_unroutable(_Peer(), "http://10.0.0.1/a.tar")


def test_the_check_is_installed_on_every_hop_not_just_the_first() -> None:
    """A redirect builds its own connection through the same handler, so the guard
    has to live where connections are made rather than in `download`. Asserted
    structurally: the wrap is on `connect`, which every hop calls."""
    source = Path(fetch.__file__).read_text(encoding="utf-8")
    assert "connection.connect = checked_connect" in source, (
        "the routability check must be installed per connection, not per download"
    )


def test_a_proxied_request_judges_the_target_not_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through a proxy the socket reaches the proxy, so its address answers a
    question nobody asked.

    A public proxy asked for `http://169.254.169.254/` was approved because the
    proxy is public; a private corporate proxy was refused for every public target
    for the mirror-image reason. Both wrong, in opposite directions, and neither
    visible from the peer address.
    """
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("169.254.169.254", 80))],
    )
    with pytest.raises(ResponseError, match="not a routable public address"):
        fetch._refuse_unroutable_origin("metadata.example", "http://metadata.example/")


def test_a_proxied_request_to_a_public_target_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction: a private proxy must not make every public fetch fail."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))],
    )
    fetch._refuse_unroutable_origin("example.com", "http://example.com/a.tar")


def test_every_resolved_address_has_to_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not merely the first. A name answering with one public and one private
    address would otherwise depend on resolver ordering, which is not a security
    boundary."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", 80)),
            (2, 1, 6, "", ("127.0.0.1", 80)),
        ],
    )
    with pytest.raises(ResponseError, match="not a routable public address"):
        fetch._refuse_unroutable_origin("split.example", "http://split.example/")


def test_a_name_that_does_not_resolve_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused rather than allowed through. A resolution failure is not evidence
    that the target is safe, and the fetch would fail immediately afterwards
    anyway -- with a worse message."""

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(ResponseError, match="could not be resolved"):
        fetch._refuse_unroutable_origin("nowhere.invalid", "http://nowhere.invalid/")


def test_the_direct_path_still_judges_the_socket() -> None:
    """The guard: without a proxy the peer address is the right thing to judge, and
    it is the only one that survives DNS rebinding. The proxy branch must not
    replace it."""
    source = Path(fetch.__file__).read_text(encoding="utf-8")
    assert "if not proxied:" in source, (
        "the socket check must still run when no proxy is involved"
    )
