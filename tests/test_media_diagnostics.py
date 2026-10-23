import json

from unicornio_editor.media.diagnostics import sanitize_candidate_diagnostics


def test_candidate_diagnostics_keep_scores_and_field_evidence_without_urls_or_text():
    source = {
        "candidate_id": "a" * 20,
        "engine": "bing",
        "direct_image_url": "https://cdn.example/art.webp?token=signed-image-secret",
        "source_page_url": "https://publisher.example/story?signature=signed-source-secret",
        "evidence_score": 9,
        "valid": True,
        "source_comparison_proved": True,
        "comparisons_attempted": 2,
        "verification_status": "verified",
        "evidence": {
            "score": 9,
            "local_score": 8,
            "gate": "relevance",
            "verdict": "deterministic_match",
            "matched": ["filename", "alt_original", "not_a_supported_field"],
            "evidence": {"alt_original": "private author detail"},
        },
        "private_payload": "must-not-be-copied",
    }

    rows = sanitize_candidate_diagnostics([source])
    serialized = json.dumps(rows)

    assert rows == [{
        "candidate_id": "a" * 20,
        "engine": "bing",
        "verdict": "deterministic_match",
        "gate": "relevance",
        "evidence_score": 9,
        "local_score": 8,
        "matched_fields": ["alt_original", "filename"],
        "source_verification": "verified",
        "source_verification_reason": "",
        "comparisons_attempted": 2,
        "source_comparison_proved": True,
        "recoverable": False,
        "needs_vision": False,
        "source_available": True,
        "source_verified": True,
    }]
    assert "https://" not in serialized
    assert "signed-image-secret" not in serialized
    assert "signed-source-secret" not in serialized
    assert "private author detail" not in serialized
    assert "must-not-be-copied" not in serialized


def test_candidate_diagnostic_list_is_bounded():
    rows = sanitize_candidate_diagnostics([{"candidate_id": str(i)} for i in range(25)])
    assert len(rows) == 20
