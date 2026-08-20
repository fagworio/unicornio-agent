"""License evidence validation for images discovered through Google Images."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


class LicenseError(ValueError):
    """Raised when an image license cannot be verified."""


_REQUIRED = {
    "source_page_url",
    "direct_image_url",
    "author",
    "license",
    "license_url",
    "captured_at",
    "credit_text",
    "alt_text",
}
_ALLOWED_LICENSE_PREFIXES = ("cc0", "cc by", "public domain", "permission granted")


def validate_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, Mapping) or set(candidate) != _REQUIRED:
        raise LicenseError("media candidate must contain complete license evidence")
    for name in ("author", "license", "captured_at", "credit_text", "alt_text"):
        if not isinstance(candidate[name], str) or not candidate[name].strip():
            raise LicenseError(f"media candidate {name} is required")
    if not candidate["credit_text"].strip().startswith("Crédito da imagem:"):
        raise LicenseError("credit_text must use the visible image-credit format")
    source = _url(candidate["source_page_url"], "source_page_url")
    direct = _url(candidate["direct_image_url"], "direct_image_url")
    license_url = _url(candidate["license_url"], "license_url")
    if urlparse(source).hostname in {"images.google.com", "www.google.com", "google.com"}:
        raise LicenseError("Google Images preview is not an original source page")
    license_name = candidate["license"].strip().lower()
    if not license_name.startswith(_ALLOWED_LICENSE_PREFIXES):
        raise LicenseError("license is not an accepted public/permission license")
    result = dict(candidate)
    result.update(
        {
            "source_page_url": source,
            "direct_image_url": direct,
            "license_url": license_url,
            "license": candidate["license"].strip(),
        }
    )
    return result


def _url(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LicenseError(f"{name} must be a URL")
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LicenseError(f"{name} must be an absolute HTTP(S) URL")
    return value
