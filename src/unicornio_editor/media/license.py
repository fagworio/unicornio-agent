"""Validate explicit image-rights metadata separately from attribution.

Visible credit is required, but it is not evidence of permission. Candidates
must name a supported license/permission and provide its explicit reference;
the source page and image URL prove provenance, not usage rights.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
from html.parser import HTMLParser
import json
import re
import secrets
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse


class LicenseError(ValueError):
    """Raised when an image credit evidence cannot be validated."""


_RIGHTS_ATTESTATION_KEY = secrets.token_bytes(32)
_RIGHTS_ATTESTATION_VERSION = 1


class _JSONLDParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside = False
        self._parts: list[str] = []
        self.documents: list[Any] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script":
            return
        values = {name.casefold(): value or "" for name, value in attrs}
        self._inside = values.get("type", "").split(";", 1)[0].strip().casefold() == "application/ld+json"
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._inside:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "script" or not self._inside:
            return
        try:
            self.documents.append(json.loads("".join(self._parts)))
        except (TypeError, ValueError):
            pass
        self._inside = False
        self._parts = []


def _normalized_url(value: str, base_url: str = "") -> str:
    try:
        parsed = urlparse(urljoin(base_url, value.strip()))
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold()
    if port and port != (443 if parsed.scheme.casefold() == "https" else 80):
        host = f"{host}:{port}"
    return urlunparse((parsed.scheme.casefold(), host, parsed.path or "/", "", parsed.query, ""))


def _schema_context(context: Any) -> bool:
    if isinstance(context, str):
        return context.rstrip("/").casefold() in {
            "https://schema.org", "http://schema.org"
        }
    if isinstance(context, list):
        return any(_schema_context(item) for item in context)
    if isinstance(context, dict):
        vocab = str(context.get("@vocab") or "").rstrip("/").casefold()
        return vocab in {"https://schema.org", "http://schema.org"}
    return False


def _jsonld_nodes(value: Any, inherited_schema_context: bool = False):
    if isinstance(value, dict):
        has_schema_context = inherited_schema_context or _schema_context(value.get("@context"))
        yield value, has_schema_context
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _jsonld_nodes(child, has_schema_context)
    elif isinstance(value, list):
        for child in value:
            yield from _jsonld_nodes(child, inherited_schema_context)


def _schema_values(value: Any, keys: tuple[str, ...]) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _schema_values(child, keys)]
    if isinstance(value, dict):
        return [
            item
            for key in keys
            for child in (value.get(key),)
            if isinstance(child, (str, dict, list))
            for item in _schema_values(child, keys)
        ]
    return []


def _image_object_proves_license(
    document: Any,
    *,
    source_page_url: str,
    direct_image_url: str,
    license_name: str,
    license_url: str,
) -> bool:
    image_url = _normalized_url(direct_image_url, source_page_url)
    declared_license = _normalized_url(license_url, source_page_url)
    if not image_url or not declared_license:
        return False
    for node, has_schema_context in _jsonld_nodes(document):
        raw_types = node.get("@type")
        types = raw_types if isinstance(raw_types, list) else [raw_types]
        if not any(
            (
                str(value or "").rstrip("/#").casefold() == "imageobject"
                and has_schema_context
            )
            or str(value or "").rstrip("/#").casefold()
            in {"https://schema.org/imageobject", "http://schema.org/imageobject"}
            for value in types
        ):
            continue
        # `contentUrl` identifies the image bytes; generic page-level license
        # markup or an ImageObject for a different image is not sufficient.
        content_urls = _schema_values(node.get("contentUrl"), ("@id", "url", "contentUrl"))
        if image_url not in {_normalized_url(value, source_page_url) for value in content_urls}:
            continue
        license_values = _schema_values(node.get("license"), ("@id", "url"))
        matching_license = next(
            (value for value in license_values if _normalized_url(value, source_page_url) == declared_license),
            "",
        )
        if matching_license and _license_reference_matches(license_name, matching_license):
            return True
    return False


def _attestation_payload(candidate: Mapping[str, Any]) -> bytes:
    fields = {
        key: str(candidate.get(key) or "")
        for key in ("source_page_url", "direct_image_url", "author", "license", "license_url")
    }
    return json.dumps(
        {"version": _RIGHTS_ATTESTATION_VERSION, **fields},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def attest_source_rights(candidate: Mapping[str, Any], source_page_html: str) -> dict[str, Any] | None:
    """Seal license evidence only when JSON-LD binds it to this exact image.

    The caller must fetch ``source_page_html`` from ``source_page_url`` using
    the repository's safe remote-fetch path. Page-level licenses, metadata
    supplied by an agent, and licenses attached to another image are rejected.
    """
    try:
        normalized = validate_candidate(candidate)
    except LicenseError:
        return None
    parser = _JSONLDParser()
    try:
        parser.feed(str(source_page_html or ""))
    except Exception:  # malformed markup fails closed
        return None
    if not any(
        _image_object_proves_license(
            document,
            source_page_url=normalized["source_page_url"],
            direct_image_url=normalized["direct_image_url"],
            license_name=normalized["license"],
            license_url=normalized["license_url"],
        )
        for document in parser.documents
    ):
        return None
    signature = hmac.new(_RIGHTS_ATTESTATION_KEY, _attestation_payload(normalized), hashlib.sha256).hexdigest()
    return {"version": _RIGHTS_ATTESTATION_VERSION, "signature": signature}


def validate_upload_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Validate metadata and require a process-local, source-bound rights seal."""
    if not isinstance(candidate, Mapping):
        raise LicenseError("media candidate must contain complete license evidence")
    normalized = validate_candidate({key: candidate.get(key) for key in _REQUIRED})
    attestation = candidate.get("source_rights_attestation")
    if not isinstance(attestation, Mapping) or attestation.get("version") != _RIGHTS_ATTESTATION_VERSION:
        raise LicenseError("rights are not proven for this exact source image")
    signature = str(attestation.get("signature") or "")
    expected = hmac.new(_RIGHTS_ATTESTATION_KEY, _attestation_payload(normalized), hashlib.sha256).hexdigest()
    if len(signature) != 64 or not hmac.compare_digest(signature, expected):
        raise LicenseError("source-bound rights evidence is invalid or does not match this image")
    return normalized


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


def _accepted_license(value: str) -> bool:
    return _license_identity(value) is not None


def _license_identity(value: str) -> tuple[str, str | None] | None:
    """Parse only recognized, compatible license labels and optional versions."""
    normalized = " ".join(value.casefold().replace("_", " ").split())
    if normalized in {"cc0", "cc0 1.0"}:
        return "cc0", "1.0" if normalized.endswith("1.0") else None
    public_domain = re.fullmatch(
        r"public domain( mark)?(?: (\d+(?:\.\d+)?))?", normalized
    )
    if public_domain:
        family = "public_domain_mark" if public_domain.group(1) else "public_domain"
        return family, public_domain.group(2)
    cc_by = re.fullmatch(r"cc by(?:[- ]sa)?(?: (\d+(?:\.\d+)?))?", normalized)
    if cc_by:
        family = "cc_by_sa" if "sa" in normalized else "cc_by"
        return family, cc_by.group(1)
    return None


def _license_reference_matches(license_name: str, license_url: str) -> bool:
    parsed = urlparse(license_url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold()
        not in {"creativecommons.org", "www.creativecommons.org"}
    ):
        return False
    path = parsed.path.casefold().rstrip("/")
    if path.endswith("/legalcode"):
        path = path[: -len("/legalcode")].rstrip("/")
    identity = _license_identity(license_name)
    if identity is None:
        return False
    family, declared_version = identity
    if family == "cc0":
        return path == "/publicdomain/zero/1.0"
    if family == "public_domain_mark":
        return path == "/publicdomain/mark/1.0" and declared_version in (None, "1.0")
    if family == "public_domain":
        return (
            path in {"/publicdomain/mark/1.0", "/publicdomain/zero/1.0"}
            and declared_version in (None, "1.0")
        )
    variant = "by-sa" if family == "cc_by_sa" else "by"
    match = re.fullmatch(rf"/licenses/{variant}/(\d+(?:\.\d+)?)", path)
    return bool(match and (declared_version is None or match.group(1) == declared_version))


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
    license_name = candidate["license"].strip()
    if not _accepted_license(license_name):
        raise LicenseError(
            "license is not accepted; visible credit does not grant permission"
        )
    # Do not infer a license reference from the source page. The caller must
    # provide the official license URL that matches the declared license.
    license_url = _url(candidate["license_url"], "license_url")
    if not _license_reference_matches(license_name, license_url):
        raise LicenseError("license reference does not match a supported license")
    result = dict(candidate)
    result.update(
        {
            "source_page_url": source,
            "direct_image_url": direct,
            "license_url": license_url,
            "license": license_name,
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
