from unicornio_editor.pipeline_v2.model import BlockerCode, InlineMedia, MediaProgress, Outcome, Phase, WorkState


def mp(required, count):
    return MediaProgress(required, tuple(InlineMedia(i, f"u{i}", i) for i in range(1, count + 1)))
from unicornio_editor.pipeline_v2.session import run_session


class Store:
    def __init__(self, states): self.states = states
    def load(self, post_id): return self.states[post_id]


def test_run_session_selects_nearest_to_ready_without_reservations():
    states = {
        1: WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True, media=mp(4, 0)),
        2: WorkState(phase=Phase.MEDIA, blocker=BlockerCode.FEATURED_VISION, relevance_approved=True, media=mp(4, 4)),
    }
    class Runner:
        def __init__(self): self.ids = []
        def run_one(self, post_id, context):
            self.ids.append(post_id)
            return Outcome.retry(Phase.MEDIA, BlockerCode.INLINE_MISSING)
    runner = Runner()
    report = run_session([(1, {}), (2, {})], Store(states), runner, limit=1)
    assert runner.ids == [2]
    assert report["selected"] == 1
    assert report["processed"] == 1
