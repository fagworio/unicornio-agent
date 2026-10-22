"""Auditable safety checks for remote media URLs.

The default is audit-only: suspicious endpoints are reported to the caller but
remain reachable, which permits a measured rollout with legacy CDNs. Enforce
mode is explicit and is intended only after reviewing audit telemetry.
"""

from __future__ import annotations

import ipaddress
import http.client
import socket
from dataclasses import dataclass
from functools import partial
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPHandler,
    HTTPSHandler,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
    getproxies,
    proxy_bypass,
)


class URLSafetyError(ValueError):
    pass


@dataclass(frozen=True)
class URLSafetyFinding:
    url: str
    reason: str


def inspect_remote_url(url: str) -> URLSafetyFinding | None:
    """Return a finding for a non-public HTTP(S) endpoint; never connects."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError:
        return URLSafetyFinding(url, "URL inválida")
    if parsed.scheme not in {"http", "https"} or not host:
        return URLSafetyFinding(url, "URL não é HTTP(S) absoluta")
    if parsed.username or parsed.password:
        return URLSafetyFinding(url, "URL com credenciais embutidas")
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return URLSafetyFinding(url, "host local")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = _resolve_addresses(host, port)
    except (OSError, ValueError):
        return URLSafetyFinding(url, "DNS não resolveu o host")
    return _address_finding(url, addresses)


def enforce_remote_url(url: str, *, mode: str = "audit") -> URLSafetyFinding | None:
    if mode not in {"off", "audit", "enforce"}:
        raise URLSafetyError("URL safety mode must be off, audit or enforce")
    if mode == "off":
        return None
    finding = inspect_remote_url(url)
    if finding and mode == "enforce":
        raise URLSafetyError(finding.reason)
    return finding


def _resolve_addresses(host: str, port: int) -> list[tuple[int, int, int, tuple]]:
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _uses_configured_proxy(scheme: str, host: str) -> bool:
    proxies = getproxies()
    proxy = proxies.get(scheme) or proxies.get("all")
    return bool(proxy and not proxy_bypass(host))


def _address_finding(url: str, addresses: list[tuple[int, int, int, tuple]]) -> URLSafetyFinding | None:
    for row in addresses:
        address = ipaddress.ip_address(row[4][0].split("%", 1)[0])
        if not address.is_global:
            return URLSafetyFinding(url, f"host resolve para endereço não público ({address})")
    return None


def _connect_pinned(host: str, port: int, timeout, source_address, *, scheme: str, mode: str, audit):
    try:
        addresses = _resolve_addresses(host, port)
    except OSError as exc:
        raise URLError(exc) from exc
    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default_port = 443 if scheme == "https" else 80
    url = f"{scheme}://{url_host}" + (f":{port}" if port != default_port else "") + "/"
    finding = _address_finding(url, addresses)
    if finding is not None:
        if audit is not None:
            audit(finding)
        if mode == "enforce":
            raise URLSafetyError(finding.reason)
    if not addresses:
        raise URLError("DNS returned no addresses")
    family, socktype, proto, _canonname, sockaddr = addresses[0]
    sock = socket.socket(family, socktype, proto)
    try:
        if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        sock.connect(sockaddr)
        return sock
    except Exception:
        sock.close()
        raise


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *args, mode="audit", audit=None, **kwargs):
        self._url_mode = mode
        self._url_audit = audit
        super().__init__(host, *args, **kwargs)

    def connect(self):
        if self._tunnel_host:
            raise URLSafetyError("proxy tunnels are disabled for remote media requests")
        self.sock = _connect_pinned(
            self.host, self.port or 80, self.timeout, self.source_address,
            scheme="http", mode=self._url_mode, audit=self._url_audit,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *args, mode="audit", audit=None, **kwargs):
        self._url_mode = mode
        self._url_audit = audit
        super().__init__(host, *args, **kwargs)

    def connect(self):
        if self._tunnel_host:
            raise URLSafetyError("proxy tunnels are disabled for remote media requests")
        raw = _connect_pinned(
            self.host, self.port or 443, self.timeout, self.source_address,
            scheme="https", mode=self._url_mode, audit=self._url_audit,
        )
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class _SafeRedirectHandler(HTTPRedirectHandler):
    handler_order = HTTPRedirectHandler.handler_order - 1

    def __init__(self, mode: str, audit=None):
        super().__init__()
        self.mode = mode
        self.audit = audit

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise URLSafetyError("redirect target is not an absolute HTTP(S) URL")
        finding = enforce_remote_url(newurl, mode=self.mode)
        if finding is not None and self.audit is not None:
            self.audit(finding)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen_remote(
    request: Request | str,
    *,
    timeout: float = 30.0,
    mode: str = "audit",
    audit=None,
):
    """Open remote HTTP(S), validating every hop and pinning resolved IPs.

    DNS is resolved at connection time, checked under ``mode``, and the socket
    connects to one of those exact addresses, preventing a second DNS lookup
    from rebinding the host after validation. A configured proxy is refused
    unless the host is explicitly bypassed, because proxy-side DNS would evade
    the pin. Explicit ``off`` mode keeps urllib's normal proxy behavior.
    """
    if mode not in {"off", "audit", "enforce"}:
        raise URLSafetyError("URL safety mode must be off, audit or enforce")
    if isinstance(request, str):
        request = Request(request)
    initial = request.full_url
    parsed = urlparse(initial)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise URLSafetyError("URL não é HTTP(S) absoluta")
    if mode == "off":
        return build_opener().open(request, timeout=timeout)
    if _uses_configured_proxy(parsed.scheme, parsed.hostname):
        finding = URLSafetyFinding(
            initial,
            "proxy configurado impede validar e fixar o destino DNS remoto",
        )
        if audit is not None:
            audit(finding)
        raise URLSafetyError(finding.reason)
    finding = enforce_remote_url(initial, mode=mode)
    if finding is not None and audit is not None:
        audit(finding)

    class SafeHTTPHandler(HTTPHandler):
        def http_open(self, req):
            factory = partial(_PinnedHTTPConnection, mode=mode, audit=audit)
            return self.do_open(factory, req)

    class SafeHTTPSHandler(HTTPSHandler):
        def https_open(self, req):
            factory = partial(_PinnedHTTPSConnection, mode=mode, audit=audit)
            return self.do_open(factory, req, context=self._context)

    opener = build_opener(
        ProxyHandler({}),
        SafeHTTPHandler(),
        SafeHTTPSHandler(),
        _SafeRedirectHandler(mode, audit),
    )
    return opener.open(request, timeout=timeout)
