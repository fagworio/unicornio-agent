from unicornio_editor.pipeline_v2.model import BlockerCode, Phase, WorkState
from unicornio_editor.pipeline_v2.runner import PipelineRunner


class MemoryStore:
    def __init__(self, state=None):
        self.state = state or WorkState()
        self.commits = []
    def load(self, post_id):
        return self.state
    def commit(self, post_id, state):
        self.state = state
        self.commits.append((post_id, state))


def test_runner_processes_one_post_and_persists_ready():
    store = MemoryStore()
    calls = []
    stages = {
        "editorial": lambda context, state: {"decision": "process"},
        "media": lambda context, state, editorial: (calls.append("media") or {}),
        "compose": lambda context, editorial, media: (calls.append("compose") or {}),
        "validate": lambda context, candidate: {"passed": True, "failures": []},
    }
    result = PipelineRunner(store, stages).run_one(114859, {"title": "test"})
    assert result.type.value == "ready"
    assert calls == ["media", "compose"]
    assert store.state.state.value == "ready"
    assert store.commits[-1][0] == 114859


def test_runner_persists_retry_without_running_later_stages_for_skip():
    store = MemoryStore()
    stages = {
        "editorial": lambda context, state: {"decision": "skip"},
        "media": lambda *args: (_ for _ in ()).throw(AssertionError("media must not run")),
        "compose": lambda *args: (_ for _ in ()).throw(AssertionError("compose must not run")),
        "validate": lambda *args: (_ for _ in ()).throw(AssertionError("validate must not run")),
    }
    result = PipelineRunner(store, stages).run_one(1, {})
    assert result.type.value == "skipped"
    assert store.state.state.value == "skipped"


def test_runner_exposes_media_blocker_as_pending_retry():
    store = MemoryStore()
    stages = {
        "editorial": lambda context, state: {"decision": "process"},
        "media": lambda context, state, editorial: {},
        "compose": lambda context, editorial, media: {},
        "validate": lambda context, candidate: {"passed": False, "failures": [{"gate": "imagens_visao"}]},
    }
    result = PipelineRunner(store, stages).run_one(114849, {})
    assert result.blocker is BlockerCode.FEATURED_VISION
    assert store.state.phase is Phase.MEDIA
    assert store.state.state is WorkState().state
