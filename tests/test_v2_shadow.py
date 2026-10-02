from unicornio_editor.pipeline_v2.model import OutcomeType
from unicornio_editor.pipeline_v2.shadow import compare_outcomes


def test_shadow_compare_normalizes_v1_partial_to_v2_retry():
    result = compare_outcomes(114849, "partial", "featured_vision", OutcomeType.RETRY, "featured_vision")
    assert result["equivalent"] is True


def test_shadow_compare_detects_real_divergence():
    result = compare_outcomes(1, "ready", None, OutcomeType.RETRY, "inline_missing")
    assert result["equivalent"] is False
