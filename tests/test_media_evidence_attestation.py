from unicornio_editor.media.evidence_attestation import attest_deterministic_media
from unicornio_editor.workflow import _item_evidence_relevant


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
        "author": "Nintendo",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-10-01T00:00:00Z",
        "credit_text": "Crédito da imagem: Nintendo. Licença CC BY 4.0.",
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


def test_attestation_is_bound_to_source_subject_and_role():
    original = _candidate()
    original["source_evidence_attestation"] = attest_deterministic_media(original)
    for field, value in (
        ("source_page_url", "https://other.test/story"),
        ("subject", "Halo Infinite"),
        ("role", "featured"),
        ("license_url", "https://other.test/terms"),
        ("license", "Uso com crédito"),
    ):
        changed = {**original, field: value}
        assert not _item_evidence_relevant(changed, {"metroid prime 4"})


def test_agent_supplied_verdict_without_runtime_seal_does_not_bypass_url_gate():
    item = _candidate()
    item["evidence"]["verdict"] = "deterministic_match"
    item["evidence"]["score"] = 9
    item["evidence"]["local_score"] = 4
    assert not _item_evidence_relevant(item, {"metroid prime 4"})
