"""Validate explicit image-rights metadata separately from attribution.

Visible credit is required, but it is not evidence of permission. Candidates
must name a supported license/permission and provide its explicit reference;
the source page and image URL prove provenance, not usage rights.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse


class LicenseError(ValueError):
    """Raised when an image credit evidence cannot be validated."""


_RIGHTS_ATTESTATION_VERSION = 2
RIGHTS_POLICY_VERSION = "source-rights-v1"
_RIGHTS_STATUSES = {
    "VERIFIED", "UNPROVEN", "INCOMPATIBLE", "UNSUPPORTED_EVIDENCE",
    "TECHNICAL_FAILURE",
}


@dataclass(frozen=True)
class RightsDecision:
    """Serializable, conservative rights result for one exact media asset."""

    status: str
    reason_code: str
    basis: str
    retryable: bool = False
    evidence_reference: str = ""
    policy_version: str = RIGHTS_POLICY_VERSION
    stage: str = "rights"

    def __post_init__(self) -> None:
        if self.status not in _RIGHTS_STATUSES:
            raise ValueError(f"unsupported rights status: {self.status}")
        if not self.reason_code or not self.basis or self.stage != "rights":
            raise ValueError("rights decision requires a reason, basis, and rights stage")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "basis": self.basis,
            "retryable": bool(self.retryable),
            "evidence_reference": self.evidence_reference,
            "policy_version": self.policy_version,
            "stage": self.stage,
        }

    @classmethod
    def from_value(cls, value: Any) -> "RightsDecision":
        """Read current decisions while treating absent/unknown legacy data as unproven."""
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            try:
                return cls(
                    status=str(value.get("status") or "UNPROVEN"),
                    reason_code=str(value.get("reason_code") or "media_rights_unproven"),
                    basis=str(value.get("basis") or "legacy_or_missing_evidence"),
                    retryable=value.get("retryable") is True,
                    evidence_reference=str(value.get("evidence_reference") or ""),
                    policy_version=str(value.get("policy_version") or RIGHTS_POLICY_VERSION),
                    stage=str(value.get("stage") or "rights"),
                )
            except (TypeError, ValueError):
                pass
        return cls(
            status="UNPROVEN",
            reason_code="media_rights_unproven",
            basis="legacy_or_missing_evidence",
        )


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


def _schema_name_values(value: Any) -> list[str]:
    """Read only explicit names/values from schema authors or creators."""
    if isinstance(value, str):
        cleaned = " ".join(re.sub(r"<[^>]+>", " ", value).split())
        if urlparse(cleaned).scheme.casefold() in {"http", "https"}:
            return []
        return [cleaned] if cleaned else []
    if isinstance(value, list):
        return [name for child in value for name in _schema_name_values(child)]
    if isinstance(value, dict):
        names: list[str] = []
        for key in ("name", "@value"):
            if key in value:
                names.extend(_schema_name_values(value[key]))
        return names
    return []


def _license_from_schema_url(value: str, source_page_url: str) -> tuple[str, str] | None:
    """Map only known Creative Commons URLs to the existing accepted policy."""
    license_url = _normalized_url(value, source_page_url)
    parsed = urlparse(license_url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in {
        "creativecommons.org", "www.creativecommons.org",
    }:
        return None
    path = parsed.path.casefold().rstrip("/")
    if path.endswith("/legalcode"):
        path = path[: -len("/legalcode")].rstrip("/")
    if re.fullmatch(r"/licenses/by-sa/\d+(?:\.\d+)?", path):
        version = path.rsplit("/", 1)[-1]
        return f"CC BY-SA {version}", license_url
    if re.fullmatch(r"/licenses/by/\d+(?:\.\d+)?", path):
        version = path.rsplit("/", 1)[-1]
        return f"CC BY {version}", license_url
    if path == "/publicdomain/zero/1.0":
        return "CC0 1.0", license_url
    if path == "/publicdomain/mark/1.0":
        return "Public Domain Mark 1.0", license_url
    return None


def _is_known_incompatible_license_url(value: str, source_page_url: str) -> bool:
    normalized = _normalized_url(value, source_page_url)
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in {
        "creativecommons.org", "www.creativecommons.org",
    }:
        return False
    path = parsed.path.casefold().rstrip("/")
    if path.endswith("/legalcode"):
        path = path[: -len("/legalcode")].rstrip("/")
    return bool(re.fullmatch(
        r"/licenses/by-(?:nc(?:-sa)?|nd|nc-nd)(?:/\d+(?:\.\d+)?)?", path
    ))


def _image_object_nodes(html: str) -> list[tuple[dict[str, Any], bool]]:
    parser = _JSONLDParser()
    try:
        parser.feed(str(html or ""))
    except Exception:  # malformed source metadata is not rights evidence
        return []
    return [
        (node, has_schema_context)
        for document in parser.documents
        for node, has_schema_context in _jsonld_nodes(document)
    ]


def _is_image_object(node: Mapping[str, Any], has_schema_context: bool) -> bool:
    raw_types = node.get("@type")
    types = raw_types if isinstance(raw_types, list) else [raw_types]
    return any(
        (
            str(value or "").rstrip("/#").casefold() == "imageobject"
            and has_schema_context
        )
        or str(value or "").rstrip("/#").casefold()
        in {"https://schema.org/imageobject", "http://schema.org/imageobject"}
        for value in types
    )


def enrich_source_image_object_rights(
    candidate: Mapping[str, Any], source_page_html: str | None,
    *, root: str | Path | None = None,
) -> dict[str, Any]:
    """Attach accepted rights only when the fetched ImageObject binds exact bytes.

    This extracts a deliberately narrow subset of schema.org: exact
    ``ImageObject.contentUrl``, a supported Creative Commons ``license``, and
    an explicit ``author``/``creator`` name on that same object. It does not
    infer permission from the publisher, page-level terms, or visible credit.
    """
    result = dict(candidate)
    if source_page_html is None:
        result["rights_checked"] = True
        result["rights_verified"] = False
        result["rights_decision"] = RightsDecision(
            "TECHNICAL_FAILURE", "media_rights_unavailable", "source_page_unavailable",
            retryable=True,
        ).to_dict()
        return result

    source_url = str(result.get("source_page_url") or "")
    direct_url = str(result.get("direct_image_url") or "")
    normalized_image = _normalized_url(direct_url, source_url)
    image_objects = [
        node for node, schema_context in _image_object_nodes(source_page_html)
        if _is_image_object(node, schema_context)
        and normalized_image
        in {
            _normalized_url(value, source_url)
            for value in _schema_values(node.get("contentUrl"), ("@id", "url", "contentUrl"))
        }
    ]
    if not image_objects:
        result["rights_checked"] = True
        result["rights_verified"] = False
        result["rights_decision"] = RightsDecision(
            "UNPROVEN", "media_rights_unproven", "no_exact_image_object",
        ).to_dict()
        return result

    unsupported_evidence = False
    incompatible_license = False
    supported_evidence: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for node in image_objects:
        license_values = _schema_values(node.get("license"), ("@id", "url"))
        if not license_values:
            continue
        names = list(dict.fromkeys(
            _schema_name_values(node.get("creator"))
            + _schema_name_values(node.get("author"))
        ))
        for raw_license_url in license_values:
            license_data = _license_from_schema_url(raw_license_url, source_url)
            if license_data is None:
                incompatible_license = incompatible_license or _is_known_incompatible_license_url(
                    raw_license_url, source_url
                )
                unsupported_evidence = True
                continue
            license_name, license_url = license_data
            if not names:
                unsupported_evidence = True
                continue
            author = ", ".join(dict.fromkeys(names))
            captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            rights_candidate = {
                "source_page_url": source_url,
                "direct_image_url": direct_url,
                "author": author,
                "license": license_name,
                "license_url": license_url,
                "captured_at": captured_at,
                "credit_text": f"Crédito da imagem: {author}. Licença {license_name}.",
                "alt_text": str(result.get("alt_text") or result.get("subject") or "Imagem editorial"),
            }
            attestation = attest_source_rights(rights_candidate, source_page_html, root=root)
            if attestation is None:
                unsupported_evidence = True
                continue
            evidence_reference = str(attestation["evidence_sha256"])
            rights_candidate["rights_evidence"] = {
                "provider": "schema_org_image_object",
                "image_url": direct_url,
                "license": license_name,
                "license_url": license_url,
                "author": author,
                "evidence_sha256": evidence_reference,
            }
            supported_evidence.append((rights_candidate, attestation, evidence_reference))

    if incompatible_license:
        decision = RightsDecision(
            "INCOMPATIBLE", "media_rights_incompatible", "unsupported_commercial_or_derivative_terms",
        )
    elif unsupported_evidence or (
        len({(row[0]["license"], row[0]["license_url"]) for row in supported_evidence}) > 1
        or len({row[0]["author"] for row in supported_evidence}) > 1
    ):
        decision = RightsDecision(
            "UNSUPPORTED_EVIDENCE", "media_rights_unsupported_evidence", "unsupported_or_incomplete_image_object",
        )
    elif supported_evidence:
        rights_candidate, attestation, evidence_reference = supported_evidence[0]
        result.update(rights_candidate)
        result["rights_checked"] = True
        result["rights_verified"] = True
        result["source_rights_attestation"] = {
            **attestation,
            "provider": "schema_org_image_object",
        }
        result["rights_evidence"] = {
            "provider": "schema_org_image_object",
            "image_url": direct_url,
            "license": rights_candidate["license"],
            "license_url": rights_candidate["license_url"],
            "author": rights_candidate["author"],
            "evidence_sha256": evidence_reference,
        }
        result["rights_decision"] = RightsDecision(
            "VERIFIED", "image_object_license_match", "schema.org_image_object",
            evidence_reference=evidence_reference,
        ).to_dict()
        return result
    else:
        decision = RightsDecision(
            "UNPROVEN", "media_rights_unproven", "image_object_without_bound_license",
        )
    result["rights_checked"] = True
    result["rights_verified"] = False
    result["rights_decision"] = decision.to_dict()
    return result


def _rights_evidence_reference(evidence: Any) -> str:
    if not isinstance(evidence, Mapping):
        return ""
    supplied = str(evidence.get("evidence_sha256") or "")
    if str(evidence.get("provider") or "") == "schema_org_image_object":
        return supplied if re.fullmatch(r"[0-9a-f]{64}", supplied) else ""
    material = {str(key): value for key, value in evidence.items() if key != "evidence_sha256"}
    calculated = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if supplied and not hmac.compare_digest(supplied, calculated):
        return ""
    return supplied or calculated


def _rights_key(root: str | Path | None) -> bytes:
    """Load or create a stable, private per-installation HMAC key."""
    configured_root = root or os.environ.get("UNICORNIO_AGENT_ROOT") or Path.cwd()
    work_dir = Path(configured_root).expanduser() / "work"
    if work_dir.is_symlink():
        raise LicenseError("rights attestation store cannot be a symlink")
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    work_metadata = work_dir.stat()
    if not work_dir.is_dir() or work_metadata.st_uid != os.geteuid():
        raise LicenseError("rights attestation store is not owned by the current user")
    key_path = work_dir / "rights-attestation.key"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    except FileExistsError:
        descriptor = -1
    if descriptor >= 0:
        try:
            key = os.urandom(32)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            directory_fd = os.open(
                work_dir,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return key
        except Exception:
            try:
                key_path.unlink()
            except OSError:
                pass
            raise

    for _ in range(20):
        try:
            metadata = key_path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise LicenseError("rights attestation key must be a private regular file")
            descriptor = os.open(key_path, os.O_RDONLY | nofollow)
            with os.fdopen(descriptor, "rb") as stream:
                key = stream.read(33)
            if len(key) == 32:
                return key
        except FileNotFoundError:
            time.sleep(0.01)
    raise LicenseError("rights attestation key is missing or malformed")


def _matching_source_evidence_hash(
    documents: list[Any], *, source_page_url: str, direct_image_url: str,
    author: str, license_name: str, license_url: str,
) -> str:
    image_url = _normalized_url(direct_image_url, source_page_url)
    declared_license = _normalized_url(license_url, source_page_url)
    declared_author = " ".join(str(author or "").split())
    if not image_url or not declared_license or not declared_author:
        return ""
    evidence_hashes: list[str] = []
    authors: set[str] = set()
    licenses: set[tuple[str, str]] = set()
    unsupported_evidence = False
    incompatible_license = False
    for document in documents:
        for node, has_schema_context in _jsonld_nodes(document):
            if not _is_image_object(node, has_schema_context):
                continue
            content_urls = _schema_values(node.get("contentUrl"), ("@id", "url", "contentUrl"))
            if image_url not in {_normalized_url(value, source_page_url) for value in content_urls}:
                continue
            license_values = _schema_values(node.get("license"), ("@id", "url"))
            # Keep the existing rule that an unlicensed duplicate object does
            # not invalidate a separate, explicitly licensed object. Every
            # license declaration on a licensed object, however, is checked.
            if not license_values:
                continue
            names = list(dict.fromkeys(
                _schema_name_values(node.get("creator"))
                + _schema_name_values(node.get("author"))
            ))
            current_author = " ".join(", ".join(names).split())
            if not current_author:
                unsupported_evidence = True
            else:
                authors.add(current_author)
                if current_author != declared_author:
                    unsupported_evidence = True

            node_matches_candidate = False
            for raw_license_url in license_values:
                license_data = _license_from_schema_url(raw_license_url, source_page_url)
                if license_data is None:
                    incompatible_license = incompatible_license or _is_known_incompatible_license_url(
                        raw_license_url, source_page_url
                    )
                    unsupported_evidence = True
                    continue
                current_name, current_url = license_data
                licenses.add((current_name, _normalized_url(current_url, source_page_url)))
                if (
                    _normalized_url(raw_license_url, source_page_url) == declared_license
                    and _license_reference_matches(license_name, raw_license_url)
                ):
                    node_matches_candidate = True
            if node_matches_candidate:
                evidence_hashes.append(hashlib.sha256(
                    json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                ).hexdigest())

    current_license_url = next(iter(licenses), ("", ""))[1]
    if (
        incompatible_license
        or unsupported_evidence
        or len(authors) != 1
        or len(licenses) != 1
        or not hmac.compare_digest(next(iter(authors), ""), declared_author)
        or not hmac.compare_digest(current_license_url, declared_license)
        or not _license_reference_matches(license_name, current_license_url)
        or not evidence_hashes
    ):
        return ""
    return evidence_hashes[0]


def _attestation_payload(candidate: Mapping[str, Any], evidence_reference: str) -> bytes:
    fields = {
        key: str(candidate.get(key) or "")
        for key in (
            "source_page_url", "direct_image_url", "author", "license", "license_url",
            "captured_at", "credit_text", "alt_text",
        )
    }
    return json.dumps(
        {
            "version": _RIGHTS_ATTESTATION_VERSION,
            "evidence_sha256": evidence_reference,
            **fields,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def attest_source_rights(
    candidate: Mapping[str, Any], source_page_html: str,
    *, root: str | Path | None = None,
) -> dict[str, Any] | None:
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
    evidence_reference = _matching_source_evidence_hash(
        parser.documents,
        source_page_url=normalized["source_page_url"],
        direct_image_url=normalized["direct_image_url"],
        author=normalized["author"],
        license_name=normalized["license"],
        license_url=normalized["license_url"],
    )
    if not evidence_reference:
        return None
    supplied_evidence = candidate.get("rights_evidence")
    if isinstance(supplied_evidence, Mapping):
        supplied_reference = str(supplied_evidence.get("evidence_sha256") or "")
        if supplied_reference and not hmac.compare_digest(supplied_reference, evidence_reference):
            return None
    try:
        key = _rights_key(root)
    except (OSError, LicenseError):
        return None
    signature = hmac.new(
        key, _attestation_payload(normalized, evidence_reference), hashlib.sha256,
    ).hexdigest()
    return {
        "version": _RIGHTS_ATTESTATION_VERSION,
        "signature": signature,
        "evidence_sha256": evidence_reference,
    }


def attest_wikimedia_rights(
    candidate: Mapping[str, Any], *, root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Seal exact-file rights returned by Wikimedia Commons extmetadata."""
    evidence = candidate.get("rights_evidence")
    if not isinstance(evidence, Mapping):
        return None
    if str(evidence.get("provider") or "") != "wikimedia_commons_api":
        return None
    if str(evidence.get("image_url") or "") != str(candidate.get("direct_image_url") or ""):
        return None
    if any(str(evidence.get(key) or "") != str(candidate.get(key) or "") for key in ("author", "license", "license_url")):
        return None
    try:
        normalized = validate_candidate({key: candidate.get(key) for key in _REQUIRED})
    except LicenseError:
        return None
    evidence_reference = _rights_evidence_reference(evidence)
    if not evidence_reference:
        return None
    try:
        key = _rights_key(root)
    except (OSError, LicenseError):
        return None
    signature = hmac.new(
        key, _attestation_payload(normalized, evidence_reference), hashlib.sha256,
    ).hexdigest()
    return {
        "version": _RIGHTS_ATTESTATION_VERSION,
        "signature": signature,
        "provider": "wikimedia_commons_api",
        "evidence_sha256": evidence_reference,
    }


def validate_upload_candidate(
    candidate: Mapping[str, Any], *, root: str | Path | None = None,
) -> dict[str, Any]:
    """Validate metadata and require a durable, source-bound rights seal."""
    if not isinstance(candidate, Mapping):
        raise LicenseError("media candidate must contain complete license evidence")
    normalized = validate_candidate({key: candidate.get(key) for key in _REQUIRED})
    attestation = candidate.get("source_rights_attestation")
    if not isinstance(attestation, Mapping) or attestation.get("version") != _RIGHTS_ATTESTATION_VERSION:
        raise LicenseError("rights are not proven for this exact source image")
    signature = str(attestation.get("signature") or "")
    evidence_reference = _rights_evidence_reference(candidate.get("rights_evidence"))
    attested_reference = str(attestation.get("evidence_sha256") or "")
    if not candidate.get("rights_evidence"):
        # Backward-compatible direct API callers still get a signed evidence
        # identity. Production acquisition persists the provider record too.
        evidence_reference = attested_reference
    if not evidence_reference or not hmac.compare_digest(evidence_reference, attested_reference):
        raise LicenseError("source rights evidence identity is missing or changed")
    try:
        key = _rights_key(root)
    except (OSError, LicenseError) as exc:
        raise LicenseError("durable rights attestation key is unavailable") from exc
    expected = hmac.new(
        key, _attestation_payload(normalized, evidence_reference), hashlib.sha256,
    ).hexdigest()
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
