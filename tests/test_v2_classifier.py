from unicornio_editor.pipeline_v2.classifier import classify, blocker_for_gate
from datetime import datetime, timezone

from unicornio_editor.pipeline_v2.model import BlockerCode, MediaProgress, OutcomeType, Phase, RetryInfo, WorkState
from unicornio_editor.pipeline_v2.errors import StageError


def test_gate_map_is_explicit_not_substring_matching():
    assert blocker_for_gate("imagens_no_corpo") is BlockerCode.INLINE_MISSING
    assert blocker_for_gate("imagens_visao") is BlockerCode.FEATURED_VISION
    assert blocker_for_gate("media_duplicate_confirmed") is BlockerCode.MEDIA_DUPLICATE
    assert blocker_for_gate("visual_identity_verified") is BlockerCode.MEDIA_INVALID
    assert blocker_for_gate("visual_identity_matches_current_media") is BlockerCode.MEDIA_INVALID
    assert blocker_for_gate("featured_identity_matches_current_attachment") is BlockerCode.FEATURED_INVALID
    assert blocker_for_gate("destaque_relevancia") is BlockerCode.FEATURED_INVALID
    assert blocker_for_gate("html_semantics") is BlockerCode.STRUCTURE
    assert blocker_for_gate("schema_editorial") is BlockerCode.SCHEMA
    assert blocker_for_gate("fonte_original_link") is BlockerCode.SOURCE
    assert blocker_for_gate("qualidade_texto") is BlockerCode.TEXT_QUALITY
    assert blocker_for_gate("imagem_inventada") is None


def test_all_validation_gates_passes_ready():
    outcome = classify(WorkState(relevance_approved=True), {"decision": "process"}, {}, {"passed": True, "failures": []})
    assert outcome.type is OutcomeType.READY


def test_media_only_failure_is_retry_media():
    outcome = classify(
        WorkState(relevance_approved=True),
        {"decision": "process"}, {},
        {"passed": False, "failures": [{"gate": "imagens_visao"}]},
    )
    assert outcome.type is OutcomeType.RETRY
    assert outcome.phase is Phase.MEDIA
    assert outcome.blocker is BlockerCode.FEATURED_VISION


def test_visual_identity_outage_retries_after_media_no_progress_budget():
    media = MediaProgress(required=2, visual_identity_pending=True)
    previous = WorkState(
        relevance_approved=True,
        retry=RetryInfo(attempts=5, no_progress=2),
    )

    outcome = classify(
        previous,
        {"decision": "process"},
        media,
        {"passed": False, "failures": [{"gate": "imagens_no_corpo"}]},
        no_progress=2,
        max_media_no_progress=2,
    )

    assert outcome.type is OutcomeType.RETRY
    assert outcome.phase is Phase.MEDIA
    assert outcome.blocker is BlockerCode.PROVIDER_ERROR
    assert outcome.detail == "visual identity verification temporarily unavailable"


def test_visual_identity_outage_retries_listicle_after_second_enrichment_round():
    media = MediaProgress(
        required=2,
        enrichment_round=2,
        visual_identity_pending=True,
    )
    previous = WorkState(
        relevance_approved=True,
        retry=RetryInfo(attempts=5, no_progress=2),
    )

    outcome = classify(
        previous,
        {"decision": "process"},
        media,
        {
            "passed": False,
            "failures": [{"gate": "imagens_no_corpo"}],
            "checklist": {"media_decision": {"is_listicle": True}},
        },
        no_progress=2,
        max_media_no_progress=2,
    )

    assert outcome.type is OutcomeType.RETRY
    assert outcome.phase is Phase.MEDIA
    assert outcome.blocker is BlockerCode.PROVIDER_ERROR


def test_temporary_featured_read_error_retries_provider_not_internal_error():
    outcome = classify(
        WorkState(relevance_approved=True, retry=RetryInfo(attempts=9)),
        {"decision": "process"}, {},
        {"passed": False, "failures": [{
            "gate": "featured_url", "blocker": "provider_error", "phase": "media",
            "detail": "attachment read failed",
        }]},
    )
    assert outcome.type is OutcomeType.RETRY
    assert outcome.phase is Phase.MEDIA
    assert outcome.blocker is BlockerCode.PROVIDER_ERROR


def test_editorial_retry_uses_phase_attempts_not_global_history():
    previous = WorkState(
        phase=Phase.MEDIA,
        retry=RetryInfo(attempts=7, phase_attempts=3),
        relevance_approved=True,
    )
    outcome = classify(
        previous,
        {"decision": "process"},
        {},
        {"passed": False, "failures": [{"gate": "qualidade_texto"}]},
        now=datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc),
    )
    assert outcome.type is OutcomeType.RETRY
    assert outcome.phase is Phase.EDITORIAL
    assert outcome.next_at == "2026-10-06T20:30:00+00:00"


def test_editorial_budget_escalates_only_after_three_editorial_attempts():
    previous = WorkState(
        phase=Phase.EDITORIAL,
        retry=RetryInfo(attempts=7, phase_attempts=2),
        relevance_approved=True,
    )
    outcome = classify(
        previous,
        {"decision": "process"},
        {},
        {"passed": False, "failures": [{"gate": "qualidade_texto"}]},
    )
    assert outcome.type is OutcomeType.HUMAN_REQUIRED
    assert outcome.phase is Phase.EDITORIAL
    assert outcome.blocker is BlockerCode.TEXT_QUALITY


def test_mixed_failure_is_retry_at_first_non_media_blocker():
    outcome = classify(
        WorkState(relevance_approved=True),
        {"decision": "process"}, {},
        {"passed": False, "failures": [{"gate": "imagens_no_corpo"}, {"gate": "qualidade_texto"}]},
    )
    assert outcome.type is OutcomeType.RETRY
    assert outcome.phase is Phase.EDITORIAL
    assert outcome.blocker is BlockerCode.TEXT_QUALITY


def test_relevance_skip_is_the_only_skip_path():
    outcome = classify(WorkState(), {"decision": "skip"}, {}, {"passed": False, "failures": []})
    assert outcome.type is OutcomeType.SKIPPED


def test_relevance_uncertain_repeats_require_human():
    outcome = classify(
        WorkState(phase=Phase.RELEVANCE, blocker=BlockerCode.RELEVANCE_UNCERTAIN, retry=RetryInfo(attempts=1)),
        {"decision": "uncertain"}, {}, {"passed": False, "failures": []},
    )
    assert outcome.type is OutcomeType.HUMAN_REQUIRED


def test_missing_editorial_decision_preserves_correlated_timeout_failure():
    outcome = classify(
        WorkState(), {}, {},
        {"passed": False, "failures": [{
            "gate": "provider_editorial", "blocker": "provider_error",
            "phase": "editorial", "detail": "editorial provider timed out",
            "failure_class": "timeout", "retryable": True,
        }]},
    )
    assert outcome.type is OutcomeType.RETRY
    assert outcome.phase is Phase.EDITORIAL
    assert outcome.blocker is BlockerCode.PROVIDER_ERROR
    assert "timeout" in (outcome.detail or "")
    assert "retryable=true" in (outcome.detail or "")


def test_missing_editorial_decision_preserves_schema_failure():
    outcome = classify(
        WorkState(), {}, {},
        {"passed": False, "failures": [{
            "gate": "schema_editorial", "phase": "editorial",
            "detail": "top-level has invalid fields (unknown=['post_id'])",
        }]},
    )
    assert outcome.type is OutcomeType.RETRY
    assert outcome.phase is Phase.EDITORIAL
    assert outcome.blocker is BlockerCode.SCHEMA
    assert "post_id" in (outcome.detail or "")


def test_stage_timeout_metadata_survives_classification():
    outcome = __import__("unicornio_editor.pipeline_v2.classifier", fromlist=["classify_stage_error"]).classify_stage_error(
        WorkState(),
        StageError(
            BlockerCode.PROVIDER_ERROR,
            Phase.EDITORIAL,
            "editorial provider timed out",
            failure_class="timeout",
            retryable=True,
        ),
    )
    assert outcome.type is OutcomeType.RETRY
    assert "failure_class=timeout" in (outcome.detail or "")
    assert "retryable=true" in (outcome.detail or "")
