import json

from unicornio_editor.media.diagnostics import (
    sanitize_candidate_diagnostic,
    sanitize_candidate_diagnostics,
    summarize_candidate_diagnostics,
)


def test_candidate_diagnostics_keep_scores_and_field_evidence_without_urls_or_text():
    source = {
        "candidate_id": "a" * 20,
        "attempt_id": "run-42",
        "query": "private query phrase",
        "role": "inline",
        "engine": "bing",
        "direct_image_url": "https://cdn.example/art.webp?token=signed-image-secret",
        "source_page_url": "https://publisher.example/story?signature=signed-source-secret",
        "evidence_score": 9,
        "valid": True,
        "source_comparison_proved": True,
        "comparisons_attempted": 2,
        "verification_status": "verified",
        "verification_stage": "source_reference",
        "verification_elapsed_ms": 3.2,
        "verification_stage_durations_ms": {
            "source_page_fetch": 2.1,
            "unexpected_private_key": 999,
        },
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

    rows = sanitize_candidate_diagnostics([source], role="inline")
    serialized = json.dumps(rows)

    assert rows == [{
        "candidate_id": "a" * 20,
        "attempt_id": "run-42",
        "query_id": rows[0]["query_id"],
        "decision": "",
        "role": "inline",
        "engine": "bing",
        "verdict": "deterministic_match",
        "gate": "relevance",
        "evidence_score": 9,
        "local_score": 8,
        "matched_fields": ["alt_original", "filename"],
        "source_verification": "verified",
        "source_verification_stage": "source_reference",
        "source_verification_category": "source_provenance_verified",
        "source_verification_reason": "",
        "verification_elapsed_ms": 3,
        "verification_stage_durations_ms": {"source_page_fetch": 2.1},
        "comparisons_attempted": 2,
        "source_comparison_proved": True,
        "recoverable": False,
        "needs_vision": False,
        "source_reference_present": True,
        "source_verified": True,
    }]
    assert "https://" not in serialized
    assert "signed-image-secret" not in serialized
    assert "signed-source-secret" not in serialized
    assert "private author detail" not in serialized
    assert "private query phrase" not in serialized
    assert "must-not-be-copied" not in serialized


def test_candidate_diagnostic_list_is_bounded():
    rows = sanitize_candidate_diagnostics([{"candidate_id": str(i)} for i in range(25)])
    assert len(rows) == 20


def test_candidate_diagnostic_summary_correlates_attempt_and_decisions_without_urls():
    rows = [
        sanitize_candidate_diagnostic(
            {
                "candidate_id": "candidate-1",
                "query": "a private search phrase",
                "source_page_url": "https://source.test/story?token=secret",
                "source_verification_reason_code": "source_page_unavailable",
                "verification_stage": "source_page_fetch",
                "verification_stage_durations_ms": {
                    "source_page_fetch": 12.3,
                    "asset_extraction": 2.2,
                    "private_stage": 900,
                },
                "evidence": {"verdict": "unresolved_source", "gate": "source_verification"},
            },
            decision="deferred",
            attempt_id="cycle-run-9",
        ),
        sanitize_candidate_diagnostic(
            {"candidate_id": "candidate-2", "evidence": {"verdict": "reject", "gate": "rights"}},
            decision="rejected",
            attempt_id="cycle-run-9",
        ),
    ]
    summary = summarize_candidate_diagnostics(rows)
    serialized = json.dumps({"rows": rows, "summary": summary})

    assert summary["candidates_seen"] == 2
    assert summary["unique_candidate_ids"] == 2
    assert summary["decisions"] == {"deferred": 1, "rejected": 1}
    assert summary["source_verification_categories"]["source_inaccessible"] == 1
    assert summary["source_verification_reasons"] == {"source_page_unavailable": 1}
    assert summary["verification_stages"] == {
        "source_page_fetch": 1,
        "unknown": 1,
    }
    assert summary["verification_stage_duration_totals_ms"] == {
        "asset_extraction": 2.2,
        "source_page_fetch": 12.3,
    }
    assert summary["providers"] == {"unknown": 2}
    assert summary["roles"] == {}
    assert summary["source_verification_states"]["unverified"] == 2
    assert rows[0]["attempt_id"] == "cycle-run-9"
    assert "secret" not in serialized
    assert "private search phrase" not in serialized
    assert "https://" not in serialized
