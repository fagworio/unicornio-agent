from unicornio_editor.pipeline_v2.legacy import from_legacy_state
from unicornio_editor.pipeline_v2.operational import _merge_ready_media


def test_ready_projection_keeps_initial_and_new_media_identity():
    initial = from_legacy_state({"state": "partial", "partial_kind": "inline_missing", "partial_required": 2, "partial_completed": 1, "partial_missing": 1}, inline_assets=[{"media_id": 114908, "media_url": "u1", "slot": 0}])
    ready = from_legacy_state({"state": "ready"})
    projected = _merge_ready_media(initial, {"status": "ready", "media_plan_results": [{"media_id": 114914, "media_url": "u2", "paragraph_index": 3, "alt_text": "a", "credit_text": "c"}]}, ready)
    assert projected.media.required == 2
    assert projected.media.accepted == 2
    assert [item.media_id for item in projected.media.inline] == [114908, 114914]
    assert projected.media.missing == 0
