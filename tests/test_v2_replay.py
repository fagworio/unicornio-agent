import json
from pathlib import Path

from unicornio_editor.pipeline_v2.model import MediaProgress, OutcomeType
from unicornio_editor.pipeline_v2.replay import replay_snapshot


def test_replay_snapshot_uses_recording_store_and_never_external_state(tmp_path):
    snapshot = {
        "post_id": 42,
        "wp": {"status": "pending", "meta": {"_hermes_state": "partial", "_hermes_partial_kind": "media", "_hermes_media_required": "1", "_hermes_media_completed": "0", "_hermes_media_missing": "1", "_hermes_last_error": "imagens_no_corpo"}, "context": {"title": {"raw": "Replay"}}},
        "manifest": {"accepted_media": []},
    }
    path = Path(tmp_path) / "42.json"
    path.write_text(json.dumps(snapshot))
    stages = {
        "editorial": lambda context, state: {"decision": "process"},
        "media": lambda context, state, editorial: MediaProgress(1),
        "compose": lambda context, editorial, media: {"title": context["title"]},
        "validate": lambda context, candidate: {"passed": False, "failures": [{"gate": "imagens_no_corpo"}]},
    }
    result = replay_snapshot(path, stages)
    assert result["outcome"]["type"] == OutcomeType.RETRY.value
    assert result["recording_writes"] == 1
    assert result["proposed"]["state"] == "pending"
    assert result["proposed"]["next_action"] == "resolve_inline"
    assert result["context"]["title"]["raw"] == "Replay"
