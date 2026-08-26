"""Input-source resolution: which files and which URLs the worker accepts. -- urls."""

from __future__ import annotations

import socket

import pytest

from runpod_doc_worker.transport import net as worker_net


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


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/hosts",
        "ftp://example.com/report.pdf",
        "gopher://example.com/report.pdf",
        "example.com/report.pdf",
    ],
)
def test_require_http_url_rejects_other_schemes(url):
    with pytest.raises(ValueError, match="must be an http"):
        worker_net.require_http_url(url, field="file_url")


def test_require_http_url_rejects_missing_host():
    with pytest.raises(ValueError, match="has no host"):
        worker_net.require_http_url("http:///report.pdf", field="file_url")


def test_require_http_url_returns_host():
    assert worker_net.require_http_url(
        "https://User:pw@Example.com:8443/a/b.pdf?t=1", field="file_url"
    ) == "example.com"


def test_check_target_accepts_a_routable_host(monkeypatch):
    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {"cdn.example.com": ["93.184.216.34"]})
    worker_net.check_target("https://cdn.example.com/r.pdf", field="file_url")


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "169.254.1.5",
        "10.0.0.5",
        "192.168.1.10",
        "172.16.4.4",
        "::1",
        "::ffff:127.0.0.1",  # the same address, spelled as mapped IPv6
    ],
)
def test_check_target_rejects_non_routable_answers(monkeypatch, addr):
    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {"host.example": [addr]})
    with pytest.raises(ValueError, match="publicly routable"):
        worker_net.check_target("http://host.example/r.pdf", field="file_url")


def test_check_target_rejects_when_any_answer_is_non_routable(monkeypatch):
    # A multi-answer host is only as good as the address the client picks.
    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {"split.example": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(ValueError, match="publicly routable"):
        worker_net.check_target("http://split.example/r.pdf", field="file_url")


def test_check_target_allows_non_routable_when_opted_in(monkeypatch):
    monkeypatch.setenv("WORKER_ALLOW_LOCAL_FETCH", "1")
    _stub_resolver(monkeypatch, {"localhost": ["127.0.0.1"]})
    worker_net.check_target("http://localhost:8000/r.pdf", field="file_url")


def test_check_target_reports_resolution_failure(monkeypatch):
    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {})
    with pytest.raises(ValueError, match="could not be resolved"):
        worker_net.check_target("http://nowhere.invalid/r.pdf", field="file_url")


@pytest.mark.parametrize("host", ["2130706433", "127.1", "0x7f000001", "0177.0.0.1"])
def test_a_noncanonical_numeric_host_is_rejected(host):
    """`ipaddress` treats these as hostnames while resolvers read them as
    127.0.0.1, so the literal-address check never fired on them. On a proxied
    request nothing downstream re-checks, because the proxy does the resolving."""
    with pytest.raises(ValueError, match="dotted-quad"):
        worker_net.require_http_url(f"http://{host}/doc.pdf", field="file_url")


@pytest.mark.parametrize("host", ["example.com", "cdn.example.com", "127.0.0.1", "[::1]"])
def test_an_ordinary_host_is_unaffected(host):
    worker_net.require_http_url(f"http://{host}/doc.pdf", field="file_url")


def test_a_multicast_literal_is_refused_end_to_end(monkeypatch):
    monkeypatch.delenv("WORKER_ALLOW_LOCAL_FETCH", raising=False)
    _stub_resolver(monkeypatch, {"mcast.example": ["224.0.0.1"]})
    with pytest.raises(ValueError, match="publicly routable"):
        worker_net.check_target("http://mcast.example/r.pdf", field="file_url")
