import json
import pytest
from pathlib import Path

from unicornio_editor.pipeline_v2.snapshot import capture_snapshot, compare_snapshot


def test_capture_snapshot_is_read_only_and_writes_immutable_input(tmp_path):
    calls = []
    path = capture_snapshot(114893, lambda post_id: calls.append(("post", post_id)) or {"id": post_id, "status": "pending", "meta": {"_hermes_state": "partial"}}, lambda post_id: calls.append(("manifest", post_id)) or {"accepted_media": []}, tmp_path)
    assert path == tmp_path / "114893.json"
    assert calls == [("post", 114893), ("manifest", 114893)]
    payload = json.loads(path.read_text())
    assert payload["post_id"] == 114893
    assert payload["wp"]["meta"]["_hermes_state"] == "partial"


def test_compare_snapshot_runs_offline(tmp_path):
    capture_snapshot(1, lambda _: {"id": 1, "status": "pending", "meta": {"_hermes_state": "partial", "_hermes_partial_kind": "media", "_hermes_media_required": "1", "_hermes_media_completed": "1", "_hermes_media_missing": "0", "_hermes_last_error": "imagens_visao"}}, lambda _: {"accepted_media": [{"media_id": 7, "media_url": "u7", "slot": 0}]}, tmp_path)
    report = compare_snapshot(tmp_path / "1.json")
    assert report["post_id"] == 1
    assert report["state"]["v2"] == "pending"
    assert report["media"]["actual_ids"] == [7]


def test_capture_snapshot_refuses_overwrite(tmp_path):
    capture_snapshot(2, lambda _: {"status": "pending", "meta": {}}, lambda _: {}, tmp_path)
    with pytest.raises(FileExistsError):
        capture_snapshot(2, lambda _: {"status": "pending", "meta": {}}, lambda _: {}, tmp_path)
