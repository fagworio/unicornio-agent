import json

from unicornio_editor.media.diagnostics import (
    measure_media_capacity,
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
        "license": "legacy data ignored",
        "rights_decision": {"token": "private"},
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
        "official_source": False,
    }]
    for secret in (
        "https://", "signed-image-secret", "signed-source-secret",
        "private author detail", "private query phrase", "must-not-be-copied",
        "legacy data ignored", "private",
    ):
        assert secret not in serialized


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
            {"candidate_id": "candidate-2", "evidence": {"verdict": "reject", "gate": "relevance"}},
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
    assert summary["verification_stages"] == {"source_page_fetch": 1, "unknown": 1}
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


def _frame(candidate_id, phash, *, verdict="deterministic_match", valid=True, gate="relevance"):
    return {
        "candidate_id": candidate_id,
        "direct_image_url": f"https://images.example/{candidate_id}.jpg?signature=private",
        "source_page_url": "https://publisher.example/story?token=private",
        "phash": phash,
        "valid": valid,
        "evidence": {"gate": gate, "verdict": verdict},
        # Legacy legal metadata must not affect technical capacity.
        "rights_verified": False,
        "rights_decision": {"status": "UNPROVEN"},
    }


def test_media_capacity_combines_reuse_and_web_distinct_frames():
    reuse = [_frame("reuse", "0000000000000000")]
    web = [
        _frame("same-as-reuse", "0000000000000000"),
        _frame("new-web", "ffffffffffffffff"),
    ]

    capacity = measure_media_capacity(
        reuse, web, required_total=3, search_requested_web=2,
    )

    assert capacity["media_required_total"] == 3
    assert capacity["media_reuse_candidates_raw"] == 1
    assert capacity["media_reuse_distinct_frames"] == 1
    assert capacity["media_required_web"] == 2
    assert capacity["media_search_requested_web"] == 2
    assert capacity["media_web_candidates_raw"] == 2
    assert capacity["media_effective_distinct_frames"] == 2
    assert capacity["media_effective_shortfall"] == 1
    serialized = json.dumps(capacity)
    assert "https://" not in serialized
    assert "signature" not in serialized


def test_media_capacity_deduplicates_visual_frames_and_requires_relevance():
    web = [
        _frame(f"duplicate-{index}", "0000000000000000")
        for index in range(3)
    ]
    web.append(_frame("irrelevant", "ffffffffffffffff", verdict="reject"))
    web.append(_frame("unverified", "eeeeeeeeeeeeeeee", valid=False))

    capacity = measure_media_capacity(
        [], web, required_total=2, search_requested_web=2,
    )

    assert capacity["media_web_candidates_raw"] == 3
    assert capacity["media_web_distinct_frames"] == 1
    assert capacity["media_effective_distinct_frames"] == 1
    assert capacity["media_effective_shortfall"] == 1


def test_media_capacity_reports_missing_visual_identity_without_claiming_a_frame():
    candidate = _frame("no-phash", None)
    capacity = measure_media_capacity(
        [], [candidate], required_total=1, search_requested_web=1,
    )

    assert capacity["media_web_candidates_raw"] == 1
    assert capacity["media_web_distinct_frames"] == 0
    assert capacity["media_effective_shortfall"] == 1
    assert capacity["media_web_without_visual_identity"] == 1
    assert capacity["media_visual_identity_available"] is True
    assert capacity["media_visual_identity_complete"] is False


def test_media_capacity_summary_sanitizes_derived_values():
    summary = summarize_candidate_diagnostics([], media_capacity={
        "media_effective_distinct_frames": "https://private.example/?token=secret",
        "media_effective_capacity_scope": "unexpected private value",
    })

    assert summary["media_effective_distinct_frames"] is None
    assert "media_effective_capacity_scope" not in summary
    assert "https://" not in json.dumps(summary)
    assert "secret" not in json.dumps(summary)
