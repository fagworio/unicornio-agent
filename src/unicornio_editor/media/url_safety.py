"""Auditable safety checks for remote media URLs.

The default is audit-only: suspicious endpoints are reported to the caller but
remain reachable, which permits a measured rollout with legacy CDNs. Enforce
mode is explicit and is intended only after reviewing audit telemetry.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse


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
        addresses = {row[4][0] for row in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except OSError:
        return URLSafetyFinding(url, "DNS não resolveu o host")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if not address.is_global:
            return URLSafetyFinding(url, f"host resolve para endereço não público ({address})")
    return None


def enforce_remote_url(url: str, *, mode: str = "audit") -> URLSafetyFinding | None:
    if mode not in {"off", "audit", "enforce"}:
        raise URLSafetyError("URL safety mode must be off, audit or enforce")
    if mode == "off":
        return None
    finding = inspect_remote_url(url)
    if finding and mode == "enforce":
        raise URLSafetyError(finding.reason)
    return finding
