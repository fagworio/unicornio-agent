import json
from unicornio_editor.pipeline_v2.model import BlockerCode, LifecycleState, Phase, WorkState
from unicornio_editor.pipeline_v2.reset_epoch import _human_required_audit


def test_committed_readback_human_required_is_safe_reset(tmp_path):
    journal = tmp_path / "work/v2-journal"
    journal.mkdir(parents=True)
    (journal / "44.json").write_text(json.dumps({"status": "committed", "readback": True, "writer_status": "readback_confirmed"}))
    state = WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING)
    post = {"id": 44, "status": "pending", "content": {"raw": "x"}, "featured_media": 0, "meta": {"_hermes_work_state": json.dumps(state.to_dict())}}
    rows = _human_required_audit(tmp_path, [post], {}, set())
    assert rows[0]["classification"] == "SAFE_RESET"
