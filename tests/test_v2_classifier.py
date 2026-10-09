from unicornio_editor.pipeline_v2.classifier import classify, blocker_for_gate
from datetime import datetime, timezone

from unicornio_editor.pipeline_v2.model import BlockerCode, OutcomeType, Phase, RetryInfo, WorkState


def test_gate_map_is_explicit_not_substring_matching():
    assert blocker_for_gate("imagens_no_corpo") is BlockerCode.INLINE_MISSING
    assert blocker_for_gate("imagens_visao") is BlockerCode.FEATURED_VISION
    assert blocker_for_gate("media_duplicate_confirmed") is BlockerCode.MEDIA_DUPLICATE
    assert blocker_for_gate("visual_identity_verified") is BlockerCode.MEDIA_INVALID
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
        WorkState(phase=Phase.RELEVANCE, blocker=BlockerCode.RELEVANCE_UNCERTAIN, retry=__import__("unicornio_editor.pipeline_v2.model", fromlist=["RetryInfo"]).RetryInfo(attempts=1)),
        {"decision": "uncertain"}, {}, {"passed": False, "failures": []},
    )
    assert outcome.type is OutcomeType.HUMAN_REQUIRED
