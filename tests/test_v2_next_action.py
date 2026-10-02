from unicornio_editor.pipeline_v2.model import Phase, WorkState
from unicornio_editor.pipeline_v2.scheduler import next_action


def test_relevance_precedes_missing_featured():
    state = WorkState(phase=Phase.RELEVANCE)
    assert next_action(state) == "evaluate_relevance"
