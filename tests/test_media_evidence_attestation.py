from typing import Any, cast

from unicornio_editor.media.evidence_attestation import attest_deterministic_media
from unicornio_editor.workflow import _item_evidence_relevant, _media_item_rejection


def _candidate():
    return {
        "candidate_id": "candidate-1",
        "source_page_url": "https://publisher.test/metroid-story",
        "direct_image_url": "https://cdn.test/opaque-file.jpg",
        "matched_image_url": "https://cdn.test/opaque-file.jpg",
        "subject": "Metroid Prime 4",
        "role": "inline",
        "is_featured": False,
        "item_identity": "",
        "item_number": None,
        "item_title": "Metroid Prime 4",
        "item_group": "",
        "html_position": None,
        "valid": True,
        "source_verification_reason_code": "source_reference_found",
        "source_comparison_proved": False,
        "credit_text": "Legenda editorial opcional",
        "alt_text": "Metroid Prime 4 key art",
        "evidence": {
            "subject": "Metroid Prime 4",
            "verdict": "deterministic_match",
            "score": 9,
            "local_score": 4,
            "matched": ["og_title", "alt_original"],
            "evidence": {
                "filename": "opaque-file.jpg",
                "og_title": "Metroid Prime 4",
                "alt_original": "Metroid Prime 4 key art",
                "page_url": "https://publisher.test/metroid-story",
            },
        },
    }


def test_signed_deterministic_origin_evidence_passes_plan_gate():
    item = _candidate()
    item["source_evidence_attestation"] = attest_deterministic_media(item)
    assert item["source_evidence_attestation"]
    assert _item_evidence_relevant(item, {"metroid prime 4"})


def test_attestation_is_bound_to_technical_source_subject_and_role():
    original = _candidate()
    original["source_evidence_attestation"] = attest_deterministic_media(original)
    for field, value in (
        ("source_page_url", "https://other.test/story"),
        ("subject", "Halo Infinite"),
        ("alt_text", "Halo Infinite key art"),
        ("role", "featured"),
    ):
        changed = {**original, field: value}
        assert not _item_evidence_relevant(changed, {"metroid prime 4"})

    for field, value in (
        ("license_url", "https://other.test/terms"),
        ("license", "legacy terms changed"),
        ("rights_verified", False),
    ):
        changed = {**original, field: value}
        assert _item_evidence_relevant(changed, {"metroid prime 4"})


def test_old_process_seal_version_is_not_accepted():
    item = _candidate()
    seal = attest_deterministic_media(item)
    assert seal is not None
    for old_version in (1, 2):
        item["source_evidence_attestation"] = {**seal, "version": old_version}
        assert not _item_evidence_relevant(item, {"metroid prime 4"})


def test_agent_supplied_verdict_without_runtime_seal_does_not_bypass_url_gate():
    item = _candidate()
    item["evidence"]["verdict"] = "deterministic_match"
    item["evidence"]["score"] = 9
    item["evidence"]["local_score"] = 4
    assert not _item_evidence_relevant(item, {"metroid prime 4"})


def test_opaque_cdn_candidate_is_revalidated_against_origin_page_context(monkeypatch):
    item = _candidate()
    item.pop("source_evidence_attestation", None)
    item["provenance_mode"] = "source_page"
    item["source_page_verified"] = True
    page = "https://publisher.test/metroid-story"
    html = (
        "<html><head><title>Metroid Prime 4 key art</title></head><body>"
        "<article><h2>Metroid Prime 4</h2>"
        '<img src="https://cdn.test/opaque-file.jpg" alt="Metroid Prime 4 key art">'
        "</article></body></html>"
    )

    def validate(candidate, *, cache, cache_html, subject, audit, url_policy):
        assert candidate["source_page_url"] == page
        cache_html[page] = html
        cache[page] = ["https://cdn.test/opaque-file.jpg"]
        return {
            "valid": True,
            "reason_code": "source_reference_found",
            "source_comparison_proved": True,
            "recoverable": False,
        }

    monkeypatch.setattr(
        "unicornio_editor.media.source_verify.validate_discovered_candidate",
        validate,
    )
    item["source_page_url"] = page
    item["direct_image_url"] = "https://cdn.test/opaque-file.jpg"
    item["matched_image_url"] = "https://cdn.test/opaque-file.jpg"
    item["subject"] = "Metroid Prime 4"
    item["evidence"] = {
        "verdict": "deterministic_match",
        "subject": "Metroid Prime 4",
        "evidence": {
            "page_title": "Metroid Prime 4 key art",
            "alt_original": "Metroid Prime 4 key art",
            "page_url": page,
        },
    }

    reason = _media_item_rejection(
        item,
        {"metroid prime 4"},
        cast(Any, object()),
        {},
        {"title": "Metroid Prime 4"},
    )

    assert reason is None


def test_revalidation_still_rejects_an_unrelated_origin_page(monkeypatch):
    item = _candidate()
    item.pop("source_evidence_attestation", None)
    item["provenance_mode"] = "source_page"
    item["source_page_url"] = "https://publisher.test/author-profile"
    item["direct_image_url"] = "https://cdn.test/opaque-file.jpg"

    def validate(_candidate, *, cache, cache_html, subject, audit, url_policy):
        cache_html["https://publisher.test/author-profile"] = (
            "<html><head><title>Author profile</title></head><body>"
            '<img src="https://cdn.test/opaque-file.jpg" alt="Author avatar">'
            "</body></html>"
        )
        return {"valid": True, "reason_code": "source_reference_found"}

    monkeypatch.setattr(
        "unicornio_editor.media.source_verify.validate_discovered_candidate",
        validate,
    )
    reason = _media_item_rejection(
        item,
        {"metroid prime 4"},
        cast(Any, object()),
        {},
        {"title": "Metroid Prime 4"},
    )

    assert reason is not None
