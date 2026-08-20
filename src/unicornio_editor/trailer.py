"""Validation for official trailer embeds."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


class TrailerError(ValueError):
    """Raised when a trailer cannot be verified as official."""


_ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com"}


def validate_trailer(candidate: Mapping[str, Any] | None) -> dict[str, str] | None:
    if candidate is None:
        return None
    if not isinstance(candidate, Mapping) or set(candidate) != {"url", "channel_url", "official_source"}:
        raise TrailerError("trailer must contain url, channel_url and official_source")
    if candidate["official_source"] is not True:
        raise TrailerError("trailer source must be explicitly official")
    url = _url(candidate["url"], "url")
    channel_url = _url(candidate["channel_url"], "channel_url")
    url_host = (urlparse(url).hostname or "").lower()
    channel_host = (urlparse(channel_url).hostname or "").lower()
    if url_host not in _ALLOWED_HOSTS or channel_host not in _ALLOWED_HOSTS:
        raise TrailerError("trailer must use an allowlisted video platform")
    if url_host not in channel_host and channel_host not in url_host:
        raise TrailerError("trailer URL and official channel must use the same platform")
    return {"url": url, "channel_url": channel_url}


def _url(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrailerError(f"trailer {name} is required")
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise TrailerError(f"trailer {name} must be HTTPS")
    return value
