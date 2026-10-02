from unicornio_editor.pipeline_v2.stages.editorial import EditorialStage
from unicornio_editor.pipeline_v2.stages.media import MediaStage
from unicornio_editor.pipeline_v2.stages.compose import ComposeStage
from unicornio_editor.pipeline_v2.stages.validate import ValidateStage


def test_stages_are_narrow_dependency_adapters():
    assert EditorialStage(lambda context, state: {"decision": "process"})({}, None) == {"decision": "process"}
    assert MediaStage(lambda context, state, editorial: {"inline": {}})({}, None, {}) == {"inline": {}}
    assert ComposeStage(lambda context, editorial, media: {"html": "ok"})({}, {}, {}) == {"html": "ok"}
    assert ValidateStage(lambda context, candidate: {"passed": True, "failures": []})({}, {})["passed"] is True
