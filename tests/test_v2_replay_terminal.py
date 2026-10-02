import json
from pathlib import Path

from unicornio_editor.pipeline_v2.replay import replay_snapshot


def test_replay_skips_terminal_snapshot_without_stages_or_writes(tmp_path):
    path = Path(tmp_path) / "ready.json"
    path.write_text(json.dumps({"post_id": 9, "wp": {"status": "publish", "meta": {"_hermes_state": "ready"}, "context": {}}, "manifest": {}}))
    def fail_stage(*args):
        raise AssertionError("terminal replay must not execute stages")
    stages = {"editorial": fail_stage, "media": fail_stage, "compose": fail_stage, "validate": fail_stage}
    result = replay_snapshot(path, stages)
    assert result["executed"] is False
    assert result["reason"] == "terminal_state"
    assert result["recording_writes"] == 0
    assert result["wordpress_writes"] == 0
    assert result["production_writes"] == 0
