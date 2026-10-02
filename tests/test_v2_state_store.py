from unicornio_editor.pipeline_v2.model import BlockerCode, LifecycleState, Phase, WorkState
from unicornio_editor.pipeline_v2.state_store import StateStore


class MemoryBackend:
    def __init__(self):
        self.data = {}

    def get(self, post_id):
        return self.data.get(post_id)

    def put(self, post_id, value):
        self.data[post_id] = value


def test_state_store_round_trips_only_v2_work_state():
    backend = MemoryBackend()
    store = StateStore(backend)
    state = WorkState(phase=Phase.MEDIA, blocker=BlockerCode.FEATURED_VISION, relevance_approved=True)
    store.commit(7, state)
    assert store.load(7) == state
    assert backend.data[7]["_hermes_work_state"]["version"] == 2


def test_state_store_missing_post_is_pending_default():
    state = StateStore(MemoryBackend()).load(99)
    assert state.state is LifecycleState.PENDING
    assert state.phase is Phase.RELEVANCE
