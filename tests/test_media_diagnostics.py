import json

from unicornio_editor.media.diagnostics import (
    measure_rights_capacity,
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
        "official_source": False,
        "rights_status": "unassessed",
        "rights_basis": "",
        "rights_reason": "",
        "rights_retryable": False,
        "rights_attestation_current": False,
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


def test_rights_diagnostics_distinguish_proof_gaps_from_technical_failures():
    rows = sanitize_candidate_diagnostics([
        {
            "candidate_id": "open-license",
            "official_source": True,
            "rights_verified": True,
            "rights_decision": {
                "status": "VERIFIED",
                "basis": "schema.org_image_object",
                "reason_code": "image_object_license_match",
            },
        },
        {
            "candidate_id": "official-unproven",
            "official_source": True,
            "rights_decision": {
                "status": "UNPROVEN",
                "basis": "no_exact_image_object",
                "reason_code": "media_rights_unproven",
            },
            "source_page_url": "https://publisher.test/page?signature=do-not-log",
        },
        {
            "candidate_id": "stale-verified-flag",
            "rights_decision": {
                "status": "VERIFIED",
                "basis": "schema.org_image_object",
                "reason_code": "image_object_license_match",
            },
        },
        {
            "candidate_id": "rights-outage",
            "rights_decision": {
                "status": "TECHNICAL_FAILURE",
                "basis": "source_page_unavailable",
                "reason_code": "media_rights_unavailable",
                "retryable": True,
            },
        },
        {"candidate_id": "not-checked"},
    ], role="inline")
    rows[0]["decision"] = "accepted"

    summary = summarize_candidate_diagnostics(rows, needed=3)
    serialized = json.dumps({"rows": rows, "summary": summary})

    assert rows[0]["rights_status"] == "VERIFIED"
    assert rows[1]["rights_status"] == "UNPROVEN"
    assert rows[1]["official_source"] is True
    assert rows[2]["rights_status"] == "VERIFIED"
    assert rows[2]["rights_attestation_current"] is False
    assert rows[3]["rights_status"] == "TECHNICAL_FAILURE"
    assert rows[3]["rights_retryable"] is True
    assert rows[4]["rights_status"] == "unassessed"
    assert summary["rights_status_counts"] == {
        "TECHNICAL_FAILURE": 1,
        "UNPROVEN": 1,
        "VERIFIED": 2,
        "unassessed": 1,
    }
    assert summary["rights_basis_counts"] == {
        "no_exact_image_object": 1,
        "schema.org_image_object": 2,
        "source_page_unavailable": 1,
    }
    assert summary["official_candidates"] == 2
    assert summary["official_rights_unresolved"] == 1
    assert summary["open_license_verified_candidates"] == 1
    assert summary["accepted_after_rights"] == 1
    assert summary["technical_rights_failures"] == 1
    assert summary["rights_metric_scope"] == "discovered_candidates_before_vision_and_upload"
    assert summary["rights_needed_candidates"] == 3
    assert summary["rights_verified_candidates"] == 1
    assert summary["rights_verified_candidates_raw"] == 1
    assert summary["rights_verified_candidate_shortfall"] == 2
    assert summary["first_unresolved_rights_candidate"] == {
        "candidate_id": "official-unproven",
        "engine": "",
        "status": "UNPROVEN",
        "reason": "media_rights_unproven",
    }
    assert "do-not-log" not in serialized
    assert "https://" not in serialized


def test_rights_diagnostics_redact_unrecognized_values():
    row = sanitize_candidate_diagnostic({
        "rights_decision": {
            "status": "permission granted by user: secret@example.test",
            "basis": "signed grant https://private.test/terms?token=secret",
            "reason_code": "raw-error: Authorization private-token",
            "retryable": True,
        },
    })

    serialized = json.dumps(row)
    assert row["rights_status"] == "unassessed"
    assert row["rights_basis"] == "other"
    assert row["rights_reason"] == "other"
    assert "private.test" not in serialized
    assert "secret" not in serialized
    assert "private-token" not in serialized


def _rights_frame(candidate_id, phash, *, status="VERIFIED", verdict="deterministic_match"):
    return {
        "candidate_id": candidate_id,
        "direct_image_url": f"https://images.example/{candidate_id}.jpg?signature=private",
        "phash": phash,
        "valid": True,
        "rights_checked": True,
        "rights_verified": status == "VERIFIED",
        "rejected_reason": "media_rights_unproven"
        if status != "VERIFIED" else "",
        "rights_decision": {
            "status": status,
            "reason_code": "image_object_license_match"
            if status == "VERIFIED" else "media_rights_unproven",
        },
        "evidence": {"gate": "relevance", "verdict": verdict},
    }


def test_rights_capacity_combines_reuse_and_web_distinct_frames():
    reuse = [
        _rights_frame("reuse-frame", "0000000000000000"),
        _rights_frame("duplicate-reuse-frame", "0000000000000000"),
    ]
    web = [
        _rights_frame("same-as-reuse", "0000000000000000"),
        _rights_frame("new-web-frame", "ffffffffffffffff"),
    ]

    capacity = measure_rights_capacity(
        reuse,
        web,
        required_total=3,
        search_requested_web=1,
    )

    assert capacity["rights_required_total"] == 3
    assert capacity["rights_reuse_verified"] == 1
    assert capacity["rights_required_web"] == 2
    assert capacity["rights_search_requested_web"] == 1
    assert capacity["rights_reuse_verified_candidates_raw"] == 2
    assert capacity["rights_web_verified"] == 2
    assert capacity["rights_effective_verified"] == 2
    assert capacity["rights_effective_shortfall"] == 1
    assert capacity["rights_verified_candidates_raw"] == 2
    serialized = json.dumps(capacity)
    assert "https://" not in serialized
    assert "signature" not in serialized


def test_rights_capacity_counts_visual_frames_not_verified_rows():
    web = [
        _rights_frame(f"duplicate-{index}", "0000000000000000")
        for index in range(4)
    ]

    capacity = measure_rights_capacity(
        [], web, required_total=3, search_requested_web=3
    )

    assert capacity["rights_verified_candidates_raw"] == 4
    assert capacity["rights_verified_distinct_frames"] == 1
    assert capacity["rights_verified_distinct_shortfall"] == 2
    assert capacity["rights_effective_verified"] == 1
    assert capacity["rights_effective_shortfall"] == 2


def test_rights_capacity_marks_verified_candidates_without_hash_as_incomplete():
    candidate = _rights_frame("verified-without-phash", "0000000000000000")
    candidate.pop("phash")

    capacity = measure_rights_capacity(
        [], [candidate], required_total=1, search_requested_web=1
    )

    assert capacity["rights_verified_candidates_raw"] == 1
    assert capacity["rights_verified_distinct_frames"] == 0
    assert capacity["rights_effective_shortfall"] == 1
    assert capacity["rights_web_verified_without_visual_identity"] == 1
    assert capacity["rights_visual_identity_available"] is True
    assert capacity["rights_visual_identity_complete"] is False


def test_rights_proof_alone_does_not_count_as_effective_relevant_capacity():
    irrelevant = _rights_frame(
        "licensed-but-irrelevant", "0000000000000000", verdict="reject"
    )

    capacity = measure_rights_capacity(
        [], [irrelevant], required_total=1, search_requested_web=1
    )

    assert capacity["rights_verified_candidates_raw"] == 1
    assert capacity["rights_verified_distinct_frames"] == 1
    assert capacity["rights_effective_web_verified"] == 0
    assert capacity["rights_effective_verified"] == 0
    assert capacity["rights_effective_shortfall"] == 1
    assert capacity["first_effective_rights_blocker"] is None


def test_rights_diagnostics_separate_unresolved_candidate_from_effective_blocker():
    rejected_for_relevance = _rights_frame(
        "unproven-but-irrelevant", "0000000000000000",
        status="UNPROVEN", verdict="reject",
    )
    effective_blocker = _rights_frame(
        "unproven-relevant", "ffffffffffffffff", status="UNPROVEN"
    )
    duplicate_blocker = _rights_frame(
        "unproven-duplicate", "ffffffffffffffff", status="UNPROVEN"
    )
    web = [rejected_for_relevance, effective_blocker, duplicate_blocker]
    capacity = measure_rights_capacity(
        [], web, required_total=1, search_requested_web=1
    )
    rows = sanitize_candidate_diagnostics(web)

    summary = summarize_candidate_diagnostics(rows, rights_capacity=capacity)

    assert summary["first_unresolved_rights_candidate"]["candidate_id"] == "unproven-but-irrelevant"
    assert summary["first_effective_rights_blocker"]["candidate_id"] == "unproven-relevant"
    assert summary["first_rights_blocker"] == summary["first_effective_rights_blocker"]
    assert summary["first_effective_rights_blocker"]["status"] == "UNPROVEN"
    serialized = json.dumps({"rows": rows, "summary": summary})
    assert "https://" not in serialized
    assert "signature" not in serialized


def test_rights_diagnostics_do_not_report_rights_as_blocker_when_capacity_is_met():
    verified = _rights_frame("already-sufficient", "0000000000000000")
    unresolved = _rights_frame("extra-unproven", "ffffffffffffffff", status="UNPROVEN")

    capacity = measure_rights_capacity(
        [], [verified, unresolved], required_total=1, search_requested_web=1
    )

    assert capacity["rights_effective_verified"] == 1
    assert capacity["rights_effective_shortfall"] == 0
    assert capacity["first_effective_rights_blocker"] is None


def test_effective_rights_blocker_requires_source_relevance_and_rights_rejection():
    source_unverified = _rights_frame(
        "source-unverified", "0000000000000000", status="UNPROVEN"
    )
    source_unverified["valid"] = False
    not_rejected_for_rights = _rights_frame(
        "not-rights-rejected", "ffffffffffffffff", status="UNPROVEN"
    )
    not_rejected_for_rights["rejected_reason"] = "image_object_license_match"
    actual_blocker = _rights_frame(
        "rights-blocker", "f0f0f0f0f0f0f0f0", status="UNPROVEN"
    )

    capacity = measure_rights_capacity(
        [],
        [source_unverified, not_rejected_for_rights, actual_blocker],
        required_total=1,
        search_requested_web=1,
    )

    assert capacity["first_effective_rights_blocker"]["candidate_id"] == "rights-blocker"


def test_rights_capacity_summary_sanitizes_derived_values():
    summary = summarize_candidate_diagnostics([], rights_capacity={
        "rights_effective_verified": "https://private.example/?token=secret",
        "first_effective_rights_blocker": {
            "candidate_id": "candidate-1",
            "engine": "bing",
            "status": "UNPROVEN private details",
            "reason": "https://private.example/?token=secret",
        },
    })

    assert summary["rights_effective_verified"] is None
    assert summary["first_effective_rights_blocker"] == {
        "candidate_id": "candidate-1",
        "engine": "bing",
        "status": "unassessed",
        "reason": "other",
    }
    assert summary["first_rights_blocker"] == summary["first_effective_rights_blocker"]
    assert "https://" not in json.dumps(summary)
    assert "secret" not in json.dumps(summary)
