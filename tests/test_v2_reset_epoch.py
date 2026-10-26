import json
from pathlib import Path

from unicornio_editor.pipeline_v2.model import LifecycleState, Phase, WorkState
from unicornio_editor.pipeline_v2.reset_epoch import _classify, _initial_state, reset_epoch


class FakeClient:
    def __init__(self, posts):
        self.posts = {int(p["id"]): p for p in posts}
        self.writes = []

    def list_pending(self, *, page=1, per_page=100, status="pending", include=None):
        return [p for p in self.posts.values() if p.get("status") == status]

    def get_post(self, post_id):
        return self.posts[int(post_id)]

    def update_post(self, post_id, payload):
        self.writes.append((post_id, payload))
        self.posts[int(post_id)]["meta"].update(payload.get("meta") or {})
        return self.posts[int(post_id)]

    def move_to_status(self, post_id, status):
        self.posts[int(post_id)]["status"] = status
        return self.posts[int(post_id)]


def post(pid, status="pending", state=None):
    return {"id": pid, "status": status, "featured_media": 0,
            "content": {"raw": f"content-{pid}"},
            "meta": {"_hermes_work_state": json.dumps(state.to_dict())} if state else {}}


def test_initial_state_uses_current_policy_and_relevance():
    state = _initial_state()
    assert state.state is LifecycleState.PENDING
    assert state.phase is Phase.RELEVANCE
    assert state.retry.attempts == 0
    assert state.retry.next_at is None
    assert state.relevance_approved is False
    assert state.media.to_dict()["inline"]["accepted"] == []


def test_preview_is_read_only_and_protects_115757(tmp_path):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(json.dumps({"version": 1, "posts": [], "release_history": []}))
    (tmp_path / "work" / "v2-cycle").mkdir(parents=True)
    (tmp_path / "work" / "v2-cycle" / "state.json").write_text(json.dumps({"schema_version": 1, "cycle_id": "old", "status": "running", "phase": "ADMISSION", "stop_reason": "recovery_uncertain", "pending_round": {"round_number": 1}, "round_reports": [{"unresolved_posts": [{"post_id": 115757}]}]}))
    client = FakeClient([post(115757), post(12), post(99, "publish")])
    result = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "report")
    assert result["status"] == "RESET_PREVIEW_ONLY"
    assert not client.writes
    classes = {row["post_id"]: row["classification"] for row in result["posts"]}
    assert classes[115757] == "protected_transaction"
    assert classes[12] == "reset"
    assert classes[99] == "preserve_published"


def test_non_editorial_status_is_excluded():
    row = _classify(Path("."), post(1, "draft"), {}, set())
    assert row["classification"] == "exclude_status"


def test_invalid_canonical_state_is_exception():
    value = post(1)
    value["meta"]["_hermes_work_state"] = "not-json"
    row = _classify(Path("."), value, {}, set())
    assert row["classification"] == "exception"


def test_published_v2_pending_is_not_reset():
    state = WorkState(state=LifecycleState.PUBLISHED, phase=Phase.PUBLISH)
    row = _classify(Path("."), post(1, "pending", state), {}, set())
    assert row["classification"] == "preserve_published"
