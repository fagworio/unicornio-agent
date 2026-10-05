import json
from pathlib import Path

from unicornio_editor.pipeline_v2.replay_level2 import replay_snapshot_level2


def test_level2_uses_real_local_compose_and_quality_without_external_calls(tmp_path):
    path = Path(tmp_path) / "active.json"
    path.write_text(json.dumps({"post_id": 11, "wp": {"status": "pending", "meta": {"_hermes_state": "partial", "_hermes_partial_kind": "inline_missing", "_hermes_media_required": "2", "_hermes_media_completed": "0", "_hermes_media_missing": "2"}, "context": {"content": "<p>text</p>", "title": {"raw": "Replay"}, "editorial": {"decision": "process"}}}, "manifest": {}}))
    result = replay_snapshot_level2(path)
    assert result["executed"] is True
    assert result["outcome"] == {"type": "retry", "blocker": "inline_missing"}
    assert result["proposed"]["next_action"] == "resolve_inline"
    assert result["wordpress_writes"] == 0
    assert result["production_writes"] == 0
