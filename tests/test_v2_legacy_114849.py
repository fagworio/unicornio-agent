from unicornio_editor.pipeline_v2.legacy import from_legacy_state
from unicornio_editor.pipeline_v2.model import BlockerCode


def test_original_114849_legacy_markers_migrate_to_featured_vision():
    state = from_legacy_state({
        "state": "partial",
        "partial_kind": "media",
        "partial_required": 4,
        "partial_completed": 4,
        "partial_missing": 0,
        "last_error": "imagens_visao: featured rejeitada",
    }, inline_assets=[{"media_id": i, "media_url": f"u{i}", "slot": i - 1} for i in range(1, 5)])
    assert state.blocker is BlockerCode.FEATURED_VISION
    assert state.media.accepted == 4
