"""License/credit evidence validation for images discovered through Google Images.

Editorial policy (2026-08): any image found on the web may be used as long
as a VISIBLE CREDIT is attached (``Crédito da imagem: ...``). Free licenses
(CC0, CC BY, public domain) remain preferred and are accepted as before;
for every other image the agent marks the candidate as ``Uso com crédito``
(use-with-credit), which is the accepted evidence — the credit block itself
is the guarantee. The source page must still be the ORIGINAL page hosting
the image (a Google Images preview URL is never a source).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


class LicenseError(ValueError):
    """Raised when an image credit evidence cannot be validated."""


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
# Use-with-credit markers: the visible credit block is the evidence, so no
# free license is required for these images (Google Images / any web source).
_CREDIT_LICENSE_PREFIXES = (
    "uso com credito",
    "uso com crédito",
    "uso com creditos",
    "uso com créditos",
    "credito",
    "crédito",
    "creditos",
    "créditos",
    "fair use",
    "divulgacao",
    "divulgação",
    "reproducao",
    "reprodução",
    "promocional",
    "cortesia",
)


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
    if urlparse(source).hostname in {"images.google.com", "www.google.com", "google.com"}:
        raise LicenseError("Google Images preview is not an original source page")
    license_name = candidate["license"].strip().lower()
    accepted = license_name.startswith(_ALLOWED_LICENSE_PREFIXES) or license_name.startswith(
        _CREDIT_LICENSE_PREFIXES
    )
    if not accepted:
        raise LicenseError(
            "license is not accepted; use a free license (CC0/CC BY/public domain) or "
            "mark the candidate as 'Uso com crédito' (visible credit is the evidence)"
        )
    license_url = candidate["license_url"].strip()
    if not license_url:
        # Use-with-credit has no license page; the original image page is the reference.
        license_url = source
    else:
        license_url = _url(license_url, "license_url")
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
