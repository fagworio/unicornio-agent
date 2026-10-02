from unicornio_editor.pipeline_v2.model import BlockerCode, InlineMedia, MediaProgress, Phase, WorkState
from unicornio_editor.pipeline_v2.runner import PipelineRunner


class Store:
    def __init__(self, state): self.state = state
    def load(self, post_id): return self.state
    def commit(self, post_id, state): self.state = state


def test_runner_persists_media_progress_from_stage():
    store = Store(WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True, media=MediaProgress(4, tuple(InlineMedia(i, f"u{i}", i) for i in range(1, 4)))))
    stages = {
        "editorial": lambda context, state: {"decision": "process"},
        "media": lambda context, state, editorial: MediaProgress(4, tuple(InlineMedia(i, f"u{i}", i) for i in range(1, 4)), featured=__import__("unicornio_editor.pipeline_v2.model", fromlist=["FeaturedProgress"]).FeaturedProgress("valid", 9)),
        "compose": lambda context, editorial, media: {},
        "validate": lambda context, candidate: {"passed": False, "failures": [{"gate": "imagens_no_corpo"}]},
    }
    PipelineRunner(store, stages).run_one(1, {})
    assert store.state.media.required == 4
    assert store.state.media.accepted == 3
    assert store.state.media.missing == 1
    assert store.state.media.featured.status == "valid"
