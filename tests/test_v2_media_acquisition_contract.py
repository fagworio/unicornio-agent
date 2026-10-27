"""Contract tests for source-image rights from extraction to the V2 plan."""

import json
from unittest import mock

from unicornio_editor.media.license import validate_upload_candidate
from unicornio_editor.media.registry import connect, record_v2_candidate_decision
from unicornio_editor.media.source_verify import discover_article_source_candidates
from unicornio_editor.pipeline_v2.runtime import ProductionMediaResolver


PAGE_URL = "https://publisher.example/metroid-prime-4-news"
IMAGE_URL = "https://cdn.publisher.example/metroid-prime-4-key-art.jpg"
SUBJECT = "Metroid Prime 4"


def _source_html(
    *, image_url=IMAGE_URL, dom_image_url=IMAGE_URL,
    author="Nintendo",
    license_url="https://creativecommons.org/licenses/by-sa/4.0/",
):
    image_object = {
        "@context": "https://schema.org",
        "@type": "ImageObject",
        "contentUrl": image_url,
        "license": license_url,
        "creator": {"@type": "Person", "name": author},
    }
    return f"""<html><head>
      <title>Metroid Prime 4 recebe nova arte</title>
      <script type="application/ld+json">{json.dumps(image_object)}</script>
    </head><body><article><figure>
      <img src="{dom_image_url}" alt="Metroid Prime 4 key art">
      <figcaption>Arte de Metroid Prime 4</figcaption>
    </figure></article></body></html>"""


def _discover(html, *, stats=None):
    with mock.patch(
        "unicornio_editor.media.source_verify._fetch",
        return_value=html.encode("utf-8") if html is not None else None,
    ):
        return discover_article_source_candidates(
            PAGE_URL, subject=SUBJECT, stats=stats,
        )


def test_real_source_extractor_preserves_bound_rights_into_executable_v2_plan():
    candidates = _discover(_source_html())

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["rights_verified"] is True
    assert candidate["rights_decision"]["status"] == "VERIFIED"
    assert candidate["rights_decision"]["basis"] == "schema.org_image_object"
    assert candidate["rights_decision"]["evidence_reference"]
    assert candidate["author"] == "Nintendo"
    assert candidate["license"] == "CC BY-SA 4.0"
    assert candidate["license_url"] == "https://creativecommons.org/licenses/by-sa/4.0/"

    validate_upload_candidate(candidate)
    assert ProductionMediaResolver._approved_unique(candidates) == candidates
    plan_item = ProductionMediaResolver._plan_item(candidate, SUBJECT, 0, False)
    validate_upload_candidate(plan_item)
    assert plan_item["item_identity"] == ""
    assert plan_item["role"] == "inline"


def test_license_for_another_image_does_not_authorize_extracted_candidate():
    html = _source_html(image_url="https://cdn.publisher.example/other-game.jpg")
    candidates = _discover(html)

    candidate = next(row for row in candidates if row["direct_image_url"] == IMAGE_URL)
    assert candidate.get("rights_verified") is not True
    assert candidate["rights_decision"]["status"] == "UNPROVEN"
    assert candidate["rights_decision"]["reason_code"] == "media_rights_unproven"
    assert ProductionMediaResolver._approved_unique(candidates) == []


def test_opaque_asset_url_keeps_rights_when_exactly_bound_by_image_object():
    opaque_url = "https://cdn.publisher.example/assets/8f1a2c9d.webp?variant=hero"
    candidates = _discover(_source_html(image_url=opaque_url, dom_image_url=opaque_url))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["direct_image_url"] == opaque_url
    assert candidate["rights_verified"] is True
    assert candidate["rights_decision"]["status"] == "VERIFIED"
    assert ProductionMediaResolver._approved_unique(candidates) == candidates


def test_incompatible_license_is_not_promoted_to_verified_rights():
    html = _source_html(license_url="https://creativecommons.org/licenses/by-nc-sa/4.0/")
    candidates = _discover(html)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.get("rights_verified") is not True
    assert candidate["rights_decision"]["status"] == "INCOMPATIBLE"
    assert candidate["rights_decision"]["reason_code"] == "media_rights_incompatible"
    assert ProductionMediaResolver._approved_unique(candidates) == []


def test_conflicting_exact_imageobject_licenses_fail_closed():
    conflicting = {
        "@context": "https://schema.org",
        "@type": "ImageObject",
        "contentUrl": IMAGE_URL,
        "license": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        "creator": {"@type": "Person", "name": "Nintendo"},
    }
    html = _source_html() + (
        '<script type="application/ld+json">'
        + json.dumps(conflicting)
        + "</script>"
    )
    candidates = _discover(html)

    assert len(candidates) == 1
    assert candidates[0]["rights_decision"]["status"] == "INCOMPATIBLE"
    assert ProductionMediaResolver._approved_unique(candidates) == []


def test_author_profile_url_is_not_invented_as_creator_name():
    candidates = _discover(_source_html(author="https://publisher.example/team/nintendo"))

    assert len(candidates) == 1
    assert candidates[0].get("rights_verified") is not True
    assert candidates[0]["rights_decision"]["status"] == "UNSUPPORTED_EVIDENCE"
    assert ProductionMediaResolver._approved_unique(candidates) == []


def test_unavailable_source_is_reported_as_technical_not_empty_verified_search():
    stats = {}
    candidates = _discover(None, stats=stats)

    assert candidates == []
    assert stats["source_status"] == "unavailable"
    assert stats["reason_code"] == "source_page_unavailable"
    assert stats["retryable"] is True


def test_unproven_rights_decision_is_persisted_without_sensitive_urls(tmp_path):
    candidate = next(
        row for row in _discover(_source_html(image_url="https://cdn.publisher.example/other.jpg"))
        if row["direct_image_url"] == IMAGE_URL
    )
    record_v2_candidate_decision(
        tmp_path,
        post_id=115234,
        run_id="rights-contract-test",
        candidate=candidate,
        role="inline",
        disposition="reject_final",
        reason_code="media_rights_unproven",
    )

    with connect(tmp_path) as db:
        row = db.execute(
            "SELECT disposition, reason_code, evidence_reference FROM media_decisions "
            "WHERE post_id=? AND candidate_id=?",
            (115234, candidate["candidate_id"]),
        ).fetchone()
    assert row is not None
    assert tuple(row[:2]) == ("reject_final", "media_rights_unproven")
    evidence = json.loads(row[2])
    assert evidence["rights_decision"]["status"] == "UNPROVEN"
    assert evidence["decision_causes"] == ["media_rights_unproven"]
    assert candidate["direct_image_url"] not in row[2]
