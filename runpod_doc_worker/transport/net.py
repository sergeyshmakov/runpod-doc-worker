"""Outbound target checks for the URL-shaped job inputs.

A job input can carry URLs the worker acts on: the document to fetch, and —
for engines that can talk to a model server instead of loading weights
in-process — where that server lives. Both arrive as free-form strings, so a
typo reaches the network stack and comes back as a socket error 120 seconds
later, or as an httpx protocol complaint that doesn't say which field was
wrong.

This module turns such a string into a checked target first, so the job fails
immediately with a message naming the field:

* ``require_http_url`` — the shape check: a scheme the worker speaks, and a
  host to connect to. This is all ``server_url`` needs; where an operator
  points their own model server is their call.
* ``resolve_checked`` — additionally resolves the host and requires the answer
  to be a publicly routable address, which is what a document URL passed to a
  serverless worker is in practice. ``<PREFIX>_ALLOW_LOCAL_FETCH=1`` lifts that
  requirement for local development and for operators serving documents from a
  host inside their own network. The prefix is the worker's own — see
  :mod:`runpod_doc_worker.config`.
* ``CheckedTargetTransport`` — an httpx transport whose sockets are opened
  against a checked address, so the address described by the check is the
  address the request actually reaches.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from runpod_doc_worker import config as _config


ALLOWED_SCHEMES = ("http", "https")

ALLOW_LOCAL_FETCH = "ALLOW_LOCAL_FETCH"


def allow_local_targets() -> bool:
    """Whether non-routable targets are acceptable for this worker."""
    return _config.active().truthy(ALLOW_LOCAL_FETCH)


def _allow_local_hint() -> str:
    """The env var an operator would set, spelled with this worker's prefix."""
    return f"set {_config.active().env_name(ALLOW_LOCAL_FETCH)}=1 to allow this"


def _policy_hint(field: str) -> str:
    """Why setting the env var will not help for this field.

    When a caller passes ``allow_local=False`` the environment is deliberately
    ignored, so printing "set <PREFIX>_ALLOW_LOCAL_FETCH=1" sends the reader to a
    switch that cannot work -- they would set it, redeploy, and get the identical
    error. Naming the field is the actionable part: the fix is to stop sending a
    private address in it, or to put the host on this worker's allow-list if it
    has one.
    """
    return (
        f"{field} does not honour "
        f"{_config.active().env_name(ALLOW_LOCAL_FETCH)}, because its value comes "
        f"from the caller rather than from this endpoint's configuration"
    )


def require_http_url(url: str, *, field: str) -> str:
    """Return the **host** of ``url``, or raise if it isn't a usable HTTP target.

    Checked here rather than left to the HTTP client so the error names the
    input field the caller got wrong instead of surfacing a protocol-level
    complaint from a library they didn't call.

    Two things worth being explicit about, because both have cost a consumer a
    real bug:

    * This is a **shape check only** — scheme, host, and address spelling. It says
      nothing about where the host resolves to. For any URL a *caller* supplied,
      use :func:`check_target`, which does this check and the address policy in
      one call. Using this function alone leaves a request that can be aimed at
      loopback, link-local, or a cloud metadata endpoint.
    * It returns the host, **not** the URL. It reads like a validator that passes
      its input through, and it does not; assigning its result back over the URL
      silently replaces the whole URL with a bare hostname.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(
            f"{field} must be an http(s) URL; got scheme {scheme or '<none>'!r}"
        )
    host = parts.hostname
    if not host:
        raise ValueError(f"{field} has no host: {url!r}")
    if _is_noncanonical_numeric(host):
        raise ValueError(
            f"{field} host {host!r} is a legacy numeric address spelling; "
            f"write it in dotted-quad form so it means the same thing to this "
            f"worker as it does to whatever resolves it"
        )
    return host


def _is_noncanonical_numeric(host: str) -> bool:
    """Whether ``host`` is an IPv4 address written in a form only a resolver reads.

    ``2130706433``, ``127.1``, ``0x7f000001`` and ``0177.0.0.1`` all mean
    127.0.0.1 to ``inet_aton`` and to most proxies, while ``ipaddress`` refuses
    them and they therefore arrive here looking like hostnames. That gap
    matters most on the proxied path, which does not use the checked transport
    — the proxy does the resolving, so nothing downstream re-examines where the
    request actually goes.

    Rejected rather than canonicalised: these spellings are never what a
    document URL wants, and rewriting a caller's host silently is worse than
    telling them it is ambiguous.
    """
    try:
        ipaddress.ip_address(host)
        return False          # a canonical address; judged on its own merits
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
    except OSError:
        return False          # an ordinary hostname
    return True


def _addresses_for(host: str, port: int | None) -> list[str]:
    """Resolve ``host`` to the addresses a connection would actually use."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise ValueError(f"host {host!r} could not be resolved: {e}") from e
    return [info[4][0] for info in infos if info[4]]


def _is_routable(addr: str) -> bool:
    """Whether ``addr`` is a publicly routable address.

    IPv4-mapped IPv6 answers (``::ffff:a.b.c.d``) are unwrapped first so the
    same address is judged the same way however the resolver spelled it.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        # Not an address we can reason about (e.g. a scoped literal) — leave
        # the decision to the connection attempt.
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    # `is_global` reports multicast as global, in both families and on every
    # supported version, so a document URL naming 224.0.0.1 or ff02::1 passed a
    # check that advertises publicly-routable. It is the only category it gets
    # wrong: broadcast, unspecified, reserved, documentation, shared-CGNAT,
    # loopback and private are all already excluded, which is why this is one
    # extra clause rather than a rewritten predicate.
    if ip.is_multicast:
        return False
    return ip.is_global


def resolve_checked_host(
    host: str,
    port: int | None,
    *,
    field: str,
    allow_local: bool | None = None,
) -> list[str]:
    """Return the addresses ``host`` may be connected to, in resolver order.

    Every address is returned, not just the first, because a host with several
    records expects a client to work down the list — using one answer alone
    would strand a dual-stack name whose leading record this worker has no
    route to.

    ``allow_local`` overrides the operator's ``ALLOW_LOCAL_FETCH`` setting for this
    one call; ``None`` consults it as before. Pass ``False`` for a field where the
    bypass should not reach.

    The bypass exists for *documents*: an operator serving PDFs from a private
    mirror sets it, and every URL the worker fetches is then exempt. That is the
    right scope for a document URL an operator chose and the wrong one for a URL a
    *caller* supplies, and a consumer of this package shipped exactly that hole --
    with the bypass on, any job could point the worker's model-server field at
    169.254.169.254 and the address policy said nothing.

    Blocking: resolution is a synchronous DNS call.
    """
    addresses = list(dict.fromkeys(_addresses_for(host, port)))
    if not addresses:
        raise ValueError(f"host {host!r} could not be resolved: no addresses returned")
    permitted = allow_local_targets() if allow_local is None else allow_local
    if not permitted:
        # Which hint depends on *why* the target was refused. With the bypass
        # merely unset an operator can turn it on; with it refused for this field
        # they cannot, and saying otherwise costs them a deploy cycle to discover.
        hint = _allow_local_hint() if allow_local is None else _policy_hint(field)
        for addr in addresses:
            if not _is_routable(addr):
                raise ValueError(
                    f"{field} must point at a publicly routable host; "
                    f"{host!r} resolves to {addr} ({hint})"
                )
    return addresses


def resolve_checked(
    url: str, *, field: str, allow_local: bool | None = None
) -> list[str]:
    """Check ``url`` and return the addresses a connection may use.

    ``allow_local=False`` refuses the operator's ``ALLOW_LOCAL_FETCH`` bypass for
    this call — see :func:`resolve_checked_host`.
    """
    host = require_http_url(url, field=field)
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as e:
        raise ValueError(f"{field} has an invalid port: {url!r}") from e
    return resolve_checked_host(host, port, field=field, allow_local=allow_local)


def check_target(
    url: str, *, field: str, allow_local: bool | None = None
) -> None:
    """The complete check for a caller-supplied URL. Raises, or returns None.

    Shape *and* address policy: it delegates to :func:`resolve_checked`, which
    calls :func:`require_http_url` itself and then rejects any host that resolves
    somewhere non-routable unless the operator opted in. So this one call is
    everything — there is no second step to remember.

    Reach for this whenever a URL came from a job payload. ``file_url`` gets the
    policy for free, because :class:`CheckedTargetTransport` applies it at connect
    time; a URL the worker hands to an engine or a third-party client instead is
    the case that needs calling this explicitly, and two consumers of this package
    independently shipped an SSRF by using :func:`require_http_url` alone for
    exactly that.

    ``allow_local=False`` refuses the ``ALLOW_LOCAL_FETCH`` bypass for this call.
    Use it for any URL a *caller* supplies: the bypass is scoped to the whole
    worker, so an operator who turns it on for their own document mirror otherwise
    exempts every caller-supplied URL too, which is how the same consumer shipped
    the same class of hole twice.
    """
    resolve_checked(url, field=field, allow_local=allow_local)


class CheckedAddressBackend(httpcore.AnyIOBackend):
    """Open sockets only to addresses the check accepted.

    Resolving inside the call that opens the socket is what ties the verdict to
    the connection: there is no second lookup in between for the two to
    disagree about.

    The URL is deliberately left alone. Connections are pooled and reused by
    URL origin, and the TLS handshake is performed against the host in that
    origin — so substituting an address into the URL instead would make two
    different hostnames that share an address look like one origin, and the
    second one would be served over the first one's connection without a
    handshake of its own. Directing the socket keeps hostname-level pooling and
    certificate verification exactly as they are without any of this.

    ``AnyIOBackend`` rather than httpcore's private ``AutoBackend``: the worker
    always runs under asyncio, which is the backend ``AutoBackend`` would pick,
    and this one is public API.
    """

    def __init__(self, field: str) -> None:
        self._field = field

    async def _resolve_within(
        self, host: str, port: int, deadline: float | None
    ) -> list[str]:
        """Resolve and check ``host``, without outstaying ``deadline``.

        Resolution is a blocking call in a worker thread, so it is bounded here
        rather than left to run for as long as the platform resolver wants.

        Abandoning the wait does not stop the thread — ``getaddrinfo`` cannot be
        interrupted, so it holds an executor slot until the platform resolver
        gives up on its own (a few seconds, per resolv.conf, not forever). What
        this buys is that the *job* stops waiting and returns inside the budget
        it was promised. The alternative, an async resolver library, would mean a
        new runtime dependency and its own view of ``/etc/hosts`` and nsswitch —
        more change than the wait is worth.
        """
        lookup = asyncio.to_thread(
            resolve_checked_host, host, port, field=self._field
        )
        if deadline is None:
            return await lookup
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpcore.ConnectTimeout(
                f"no time left to resolve {host!r} within the fetch budget"
            )
        try:
            return await asyncio.wait_for(lookup, remaining)
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise httpcore.ConnectTimeout(
                f"resolving {host!r} outlasted the fetch budget"
            ) from e

    async def connect_tcp(  # noqa: PLR0913 — signature mirrors the base class
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        # The budget starts before the lookup, not after it. Resolution is the
        # first thing this call does and it can block for as long as the
        # platform resolver allows, so leaving it outside would let a name that
        # never answers hold a fetch open past the timeout the caller was given.
        deadline = None if timeout is None else time.monotonic() + timeout
        addresses = await self._resolve_within(host, port, deadline)
        # Work down the checked answers the way the socket layer would, so a
        # host whose leading record is unreachable from this worker still gets
        # fetched. Only a failure to open the socket moves on; anything that
        # happens after that belongs to the caller.
        #
        # The timeout is one budget for the whole call — the lookup above and
        # every address below. Handing the full value to each attempt would
        # multiply it by however many records a name happens to return, so a
        # name whose addresses all accept and then say nothing could hold the
        # fetch far past the timeout the caller was promised. The base backend
        # bounds a connect to a name with a single deadline covering every
        # address it tries; this keeps that property.
        last_error: Exception | None = None
        for index, addr in enumerate(addresses):
            attempt_timeout: float | None = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Share what is left with the answers still to come. An address
                # that accepts and then says nothing would otherwise spend the
                # whole budget on itself and strand the reachable ones behind
                # it — the case this walk exists for. Failing fast returns the
                # unspent time to the next attempt, and the last gets the lot.
                attempt_timeout = remaining / (len(addresses) - index)
            try:
                return await super().connect_tcp(
                    addr,
                    port,
                    timeout=attempt_timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last_error = e
        if last_error is None:
            # Only reachable with a budget that was already spent on arrival.
            raise httpcore.ConnectTimeout(
                f"no time left to connect to {host!r} within {timeout}s"
            )
        raise last_error


class CheckedTargetTransport(httpx.AsyncHTTPTransport):
    """An httpx transport whose sockets are opened against checked addresses.

    httpx builds its connection pool itself and takes no network backend, so
    the pool's backend is swapped after construction. The pool creates
    connections lazily and hands each one whichever backend it holds at that
    moment, so this covers every connection the transport opens — including the
    ones opened for redirects.

    ``_pool._network_backend`` is not public API. A guard test asserts both
    names still exist and that the backend's ``connect_tcp`` still has the
    signature this subclass overrides, so an httpx or httpcore upgrade that
    moves them fails in CI rather than quietly connecting unchecked.
    """

    def __init__(self, *, field: str = "file_url", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pool._network_backend = CheckedAddressBackend(field)


def environment_proxy_mounts() -> dict[str, httpx.AsyncBaseTransport | None]:
    """Per-pattern transports for whatever proxying the environment asks for.

    Supplying a transport is what stops httpx reading the proxy environment
    itself, so the reading is done here and handed back as explicit mounts. A
    ``None`` value means "no proxy for this pattern" — httpx then falls back to
    the client's own transport, which is the checked one. That is what keeps a
    request the environment does not proxy on the checked path: a ``NO_PROXY``
    host, or a scheme with no proxy set for it, arrives here as ``None`` rather
    than as an absence, and an all-or-nothing choice between the two clients
    would have sent those direct and unchecked.

    ``get_environment_proxies`` is not public API; a guard test covers it, since
    reimplementing ``NO_PROXY`` matching would be the more fragile option.
    """
    from httpx._utils import get_environment_proxies  # noqa: PLC0415

    return {
        pattern: None if url is None else httpx.AsyncHTTPTransport(proxy=url)
        for pattern, url in get_environment_proxies().items()
    }


def _literal_address(host: str) -> Any:
    """Return ``host`` as an address if it is written as one, else ``None``."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


async def request_hook(request) -> None:  # noqa: ANN001 — httpx.Request
    """httpx event hook: shape-check each outgoing request's URL.

    Runs for every request httpx makes, redirects included, so a hop that lands
    on a scheme the worker doesn't speak is reported against the field the
    caller wrote rather than as a protocol error from a library they never
    called.

    Deliberately does no resolution. Where a request connects is settled by
    :class:`CheckedAddressBackend`, and every hop that needs a socket goes
    through it: a hop reusing a pooled connection is by definition the same
    origin — same host — as the one already checked when that connection was
    opened, and a hop to a different host is a different origin, so it opens a
    new connection and gets checked. Resolving here as well would put a second
    unbounded lookup per hop outside the connect budget.

    A host written as an address is judged here anyway, because that costs no
    lookup and no budget. It is also the one case worth catching on a proxied
    request, where the proxy resolves names itself and the destination policy
    for them is the proxy's to enforce, not this worker's.
    """
    host = require_http_url(str(request.url), field="file_url")
    literal = _literal_address(host)
    if literal is not None and not allow_local_targets() and not _is_routable(host):
        raise ValueError(
            f"file_url must point at a publicly routable host; "
            f"{host!r} is not one "
            f"({_allow_local_hint()})"
        )
