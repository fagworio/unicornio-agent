from unicornio_editor.pipeline_v2.legacy import LegacyStateLoader
from unicornio_editor.pipeline_v2.model import BlockerCode, Phase
from unicornio_editor.pipeline_v2.state_store import StateStore


class Backend:
    def __init__(self, value): self.value = value
    def get(self, post_id): return self.value
    def put(self, post_id, value): self.value = value


def test_state_store_falls_back_to_v1_legacy_markers_during_transition():
    backend = Backend({
        "_hermes_state": "partial",
        "_hermes_partial_kind": "featured_vision",
        "_hermes_media_required": "4",
        "_hermes_media_completed": "4",
        "_hermes_media_missing": "0",
    })
    loader = LegacyStateLoader(lambda post_id: {
        "accepted_media": [
            {"media_id": i, "media_url": f"u{i}", "slot": i}
            for i in range(1, 5)
        ],
        "featured": {"status": "vision_rejected", "media_id": 9},
    })
    state = StateStore(backend, legacy_loader=loader).load(114849)
    assert state.phase is Phase.MEDIA
    assert state.blocker is BlockerCode.FEATURED_VISION
    assert state.media.accepted == 4
    assert [item.media_id for item in state.media.inline] == [1, 2, 3, 4]
