"""Technical source, relevance and legacy compatibility contracts for V2 media."""

import json
from unittest import mock

from unicornio_editor.media.registry import connect, record_v2_candidate_decision
from unicornio_editor.media.source_verify import discover_article_source_candidates
from unicornio_editor.pipeline_v2.runtime import (
    ProductionMediaResolver,
    _is_legacy_rights_only_media_decision,
)


PAGE_URL = "https://publisher.example/metroid-prime-4-news"
IMAGE_URL = "https://cdn.publisher.example/metroid-prime-4-key-art.jpg"
SUBJECT = "Metroid Prime 4"


def _source_html(image_url=IMAGE_URL):
    return f"""<html><head><title>Metroid Prime 4 recebe nova arte</title></head>
    <body><article><figure><img src="{image_url}" alt="Metroid Prime 4 key art">
    <figcaption>Arte de Metroid Prime 4</figcaption></figure></article></body></html>"""


def _discover(html, *, stats=None):
    with mock.patch(
        "unicornio_editor.media.source_verify._fetch",
        return_value=html.encode("utf-8") if html is not None else None,
    ):
        return discover_article_source_candidates(PAGE_URL, subject=SUBJECT, stats=stats)


def test_source_without_license_metadata_is_eligible_by_technical_evidence():
    candidates = _discover(_source_html())

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["source_page_url"] == PAGE_URL
    assert candidate["direct_image_url"] == IMAGE_URL
    assert candidate["valid"] is True
    assert candidate["source_verified"] is True
    assert candidate["evidence"]["verdict"] == "deterministic_match"
    assert ProductionMediaResolver._approved_unique(candidates) == candidates

    plan_item = ProductionMediaResolver._plan_item(candidate, SUBJECT, 0, False)
    assert plan_item["source_page_url"] == PAGE_URL
    assert plan_item["direct_image_url"] == IMAGE_URL
    assert plan_item["role"] == "inline"
    assert not any(key in plan_item for key in ("license", "license_url", "rights_verified"))


def test_unrelated_source_asset_remains_ineligible():
    html = _source_html("https://cdn.publisher.example/other-game.jpg")
    html = html.replace("Metroid Prime 4 key art", "Unrelated game key art")
    html = html.replace("Arte de Metroid Prime 4", "Imagem de outro jogo")
    candidates = _discover(html)
    assert len(candidates) == 1
    assert ProductionMediaResolver._approved_unique(candidates) == []


def test_legacy_rights_fields_are_ignored_by_candidate_eligibility():
    candidate = {
        "source_page_url": PAGE_URL,
        "direct_image_url": IMAGE_URL,
        "valid": True,
        "evidence": {"verdict": "deterministic_match"},
        "rights_verified": False,
        "rights_decision": {"status": "UNPROVEN", "reason_code": "media_rights_unproven"},
    }
    assert ProductionMediaResolver._approved_unique([candidate]) == [candidate]


def test_legacy_rights_only_rejection_is_ignored_but_nested_source_mismatch_is_kept():
    assert _is_legacy_rights_only_media_decision({
        "rejected_reason": "media_rights_unproven",
        "evidence": {"verdict": "reject", "gate": "rights"},
    })
    assert not _is_legacy_rights_only_media_decision({
        "rejected_reason": "media_rights_unproven",
        "invalid_media": [{
            "original_gate": "source_verification",
            "evidence": {"verdict": "source_mismatch"},
        }],
    })


def test_unavailable_source_is_reported_as_retryable_technical_failure():
    stats = {}
    assert _discover(None, stats=stats) == []
    assert stats["source_status"] == "unavailable"
    assert stats["reason_code"] == "source_page_unavailable"
    assert stats["retryable"] is True


def test_registry_persists_technical_decision_without_sensitive_urls(tmp_path):
    candidate = {
        "candidate_id": "candidate-safe-id",
        "source_page_url": PAGE_URL + "?signature=private-source-token",
        "direct_image_url": IMAGE_URL + "?token=private-image-token",
        "valid": True,
        "evidence": {
            "verdict": "deterministic_match",
            "gate": "relevance",
            "score": 9,
            "matched": ["filename", "alt_original"],
        },
        "license": "legacy value must not be used",
        "rights_decision": {"status": "UNPROVEN"},
    }
    record_v2_candidate_decision(
        tmp_path,
        post_id=115234,
        run_id="technical-source-contract",
        candidate=candidate,
        role="inline",
        disposition="accept",
        reason_code="candidate_accepted",
    )

    with connect(tmp_path) as db:
        row = db.execute(
            "SELECT disposition, reason_code, evidence_reference FROM media_decisions "
            "WHERE post_id=? AND candidate_id=?",
            (115234, "candidate-safe-id"),
        ).fetchone()
    assert row is not None
    assert tuple(row[:2]) == ("accept", "candidate_accepted")
    evidence = json.loads(row[2])
    serialized = json.dumps(evidence)
    assert evidence["verdict"] == "deterministic_match"
    assert "private-source-token" not in serialized
    assert "private-image-token" not in serialized
    assert "legacy value must not be used" not in serialized
    assert "rights_decision" not in serialized
