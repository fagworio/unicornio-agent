import json
from pathlib import Path

from unicornio_editor.pipeline_v2.reset_epoch import reset_epoch


class ApplyClient:
    def __init__(self, posts, fail_on=None):
        self.posts = {int(p["id"]): p for p in posts}
        self.writes = []
        self.fail_on = fail_on

    def list_pending(self, *, page=1, per_page=100, status="pending", include=None):
        return [p for p in self.posts.values() if p.get("status") == status]

    def get_post(self, post_id):
        return json.loads(json.dumps(self.posts[int(post_id)]))

    def update_post(self, post_id, payload):
        if self.fail_on == post_id and not any(row[0] == post_id for row in self.writes):
            raise RuntimeError("injected update failure")
        self.writes.append((post_id, payload))
        self.posts[int(post_id)]["meta"].update(payload.get("meta") or {})
        return self.get_post(post_id)

    def move_to_status(self, post_id, status):
        self.posts[int(post_id)]["status"] = status
        return self.get_post(post_id)


def _post(pid):
    return {"id": pid, "status": "pending", "featured_media": 0, "content": {"raw": f"content-{pid}"}, "meta": {}}


def _root(tmp_path, posts):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(json.dumps({"version": 1, "posts": [], "release_history": []}))
    (tmp_path / "work" / "v2-cycle").mkdir(parents=True)
    (tmp_path / "work" / "v2-cycle" / "state.json").write_text(json.dumps({"schema_version": 1, "cycle_id": "old", "status": "running", "phase": "ADMISSION", "round_reports": [], "config": {}}))
    (tmp_path / "work" / "v2-journal").mkdir(parents=True)
    (tmp_path / "backups" / str(posts[0]["id"])).mkdir(parents=True)
    (tmp_path / "backups" / str(posts[0]["id"]) / "editorial.draft.json").write_text('{"old":true}')
    from unicornio_editor.media.registry import connect
    db = connect(tmp_path)
    db.close()


def test_apply_archives_active_backups_and_readback(tmp_path):
    posts = [_post(12)]
    _root(tmp_path, posts)
    client = ApplyClient(posts)
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")
    result = reset_epoch(client, object(), tmp_path, preview=False, apply=True, preview_evidence=Path(preview["preview"]), operator_authorization="auth-test", report_dir=tmp_path / "apply")
    assert result["status"] == "EDITORIAL_RESET_COMPLETE"
    assert (tmp_path / "backups").exists() and not (tmp_path / "backups" / "12" / "editorial.draft.json").exists()
    assert list((tmp_path / "work" / "v2-epochs" / "archives").rglob("active-root-backups/12/editorial.draft.json"))
    assert client.posts[12]["status"] == "pending"
    assert json.loads(client.posts[12]["meta"]["_hermes_work_state"])["retry"]["attempts"] == 0
    assert not (tmp_path / "work" / "v2-maintenance.lock").exists()


def test_apply_resumes_same_operation_after_post_failure(tmp_path):
    posts = [_post(12), _post(13)]
    _root(tmp_path, posts)
    client = ApplyClient(posts, fail_on=13)
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")
    try:
        reset_epoch(client, object(), tmp_path, preview=False, apply=True, preview_evidence=Path(preview["preview"]), operator_authorization="auth-test", report_dir=tmp_path / "apply1")
    except RuntimeError as exc:
        assert "injected" in str(exc)
    client.fail_on = None
    result = reset_epoch(client, object(), tmp_path, preview=False, apply=True, preview_evidence=Path(preview["preview"]), operator_authorization="auth-test", report_dir=tmp_path / "apply2")
    assert result["status"] == "EDITORIAL_RESET_COMPLETE"
    assert [post_id for post_id, _ in client.writes].count(12) == 1
    assert [post_id for post_id, _ in client.writes].count(13) == 1
    transition = json.loads(next((tmp_path / "work" / "v2-epochs" / "transitions").glob("*.json")).read_text())
    assert transition["phase"] == "ACTIVATED"
