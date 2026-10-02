from unicornio_editor.pipeline_v2.model import BlockerCode, Outcome, Phase, WorkState
from unicornio_editor.pipeline_v2.session import run_session


class Store:
    def __init__(self): self.states = {i: WorkState() for i in range(1, 6)}
    def load(self, post_id): return self.states[post_id]


def test_session_isolates_runner_failure_and_processes_remaining_posts():
    class Runner:
        def __init__(self): self.ids = []
        def run_one(self, post_id, context):
            self.ids.append(post_id)
            if post_id == 2:
                raise RuntimeError("provider timeout")
            return Outcome.retry(Phase.RELEVANCE, BlockerCode.RELEVANCE_UNCERTAIN)
    runner = Runner()
    report = run_session([(i, {}) for i in range(1, 6)], Store(), runner, limit=5)
    assert set(runner.ids) == {1, 2, 3, 4, 5}
    assert report["processed"] == 5
    assert report["errors"] == 1
    assert next(detail for detail in report["details"] if detail["post_id"] == 2)["error"] == "provider timeout"
