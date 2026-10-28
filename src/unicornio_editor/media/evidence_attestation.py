"""Process-local seals for deterministic media evidence entering the writer.

The seal is minted only by the V2 runtime after source and relevance gates.
It lets the later plan validator consume that exact decision without trusting
free-form fields from an agent-generated plan. It is intentionally ephemeral:
persisted plans must be re-evaluated in a new process.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

_KEY = secrets.token_bytes(32)
# Version 2 deliberately excludes historical legal/license metadata. Old
# process-local seals are not reusable after restart and fail this version check.
_VERSION = 2


def _payload(item: dict[str, Any]) -> bytes:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    fields = evidence.get("evidence") if isinstance(evidence.get("evidence"), dict) else {}
    signed = {
        "version": _VERSION,
        "candidate_id": str(item.get("candidate_id") or ""),
        "source_page_url": str(item.get("source_page_url") or ""),
        "direct_image_url": str(item.get("direct_image_url") or ""),
        "matched_image_url": str(item.get("matched_image_url") or ""),
        "subject": str(item.get("subject") or ""),
        "role": str(item.get("role") or ""),
        "is_featured": bool(item.get("is_featured")),
        "item_identity": str(item.get("item_identity") or ""),
        "item_number": item.get("item_number"),
        "item_title": str(item.get("item_title") or ""),
        "item_group": str(item.get("item_group") or ""),
        "html_position": item.get("html_position"),
        "source_verification_reason_code": str(item.get("source_verification_reason_code") or ""),
        "source_comparison_proved": bool(item.get("source_comparison_proved")),
        "evidence": {
            "verdict": str(evidence.get("verdict") or ""),
            "score": evidence.get("score"),
            "local_score": evidence.get("local_score"),
            "subject": str(evidence.get("subject") or ""),
            "matched": list(evidence.get("matched") or []),
            "fields": {str(key): str(value) for key, value in fields.items()},
        },
    }
    return json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def attest_deterministic_media(item: dict[str, Any]) -> dict[str, Any] | None:
    """Seal an internally evaluated, source-verified deterministic match."""
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    try:
        score = int(evidence.get("score", 0))
        local_score = int(evidence.get("local_score", 0))
    except (TypeError, ValueError):
        return None
    if (
        evidence.get("verdict") != "deterministic_match"
        or score < 7
        or local_score < 4
        or item.get("valid") is not True
        or bool(item.get("source_verification_recoverable"))
        or not item.get("source_page_url")
        or not item.get("direct_image_url")
        or not item.get("subject")
        or item.get("needs_vision")
    ):
        return None
    signature = hmac.new(_KEY, _payload(item), hashlib.sha256).hexdigest()
    return {"version": _VERSION, "signature": signature}


def verify_deterministic_media(item: dict[str, Any]) -> bool:
    """Verify that signed source evidence still matches the exact plan item."""
    seal = item.get("source_evidence_attestation")
    if not isinstance(seal, dict) or seal.get("version") != _VERSION:
        return False
    signature = str(seal.get("signature") or "")
    if len(signature) != 64:
        return False
    expected = hmac.new(_KEY, _payload(item), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
