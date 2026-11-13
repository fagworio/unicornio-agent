import json
from pathlib import Path

import pytest

from unicornio_editor.eligibility import resolve_post_eligibility
from unicornio_editor.pipeline_v2.model import WorkState
from unicornio_editor.pipeline_v2.v1_migration import migrate_safe_v2_to_v1
from unicornio_editor.workflow import WorkflowError, apply_editorial, build_queue_report, prepare_post


def _post(post_id=1, meta=None):
    return {
        "id": post_id,
        "status": "pending",
        "date": "2026-11-13T10:00:00+00:00",
        "date_gmt": "2026-11-13T10:00:00+00:00",
        "title": {"raw": "Post de teste"},
        "content": {"raw": "<p>Conteudo original.</p>", "rendered": "<p>Conteudo original.</p>"},
        "meta": dict(meta or {}),
    }


class QueueClient:
    def __init__(self, posts):
        self.posts = posts

    def list_pending(self, **kwargs):
        if kwargs.get("status") == "awaiting_human":
            return []
        return list(self.posts)


class SingleClient:
    def __init__(self, post):
        self.post = post

    def get_post(self, _post_id):
        return self.post

    def update_post(self, _post_id, payload):
        meta = payload.get("meta") or {}
        current = self.post.setdefault("meta", {})
        for key, value in meta.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        return self.post


def test_v2_without_legacy_projection_never_becomes_v1_new(tmp_path):
    raw = json.dumps(WorkState().to_dict())
    post = _post(meta={"_hermes_work_state": raw})

    resolved = resolve_post_eligibility(post, tmp_path)
    assert resolved.is_v2
    assert resolved.state == "pending"
    assert not resolved.eligible_for_v1

    report = build_queue_report(QueueClient([post]), tmp_path)
    assert report["unprocessed_ids"] == []
    assert report["v2_eligible_ids"] == [1]
    assert report["posts"][0]["state"] == "pending"
    assert report["posts"][0]["state_source"] == "v2"
    assert report["posts"][0]["v2"] is True


def test_invalid_v2_checkpoint_is_uncertain_not_new(tmp_path):
    post = _post(meta={"_hermes_work_state": "not-json"})
    resolved = resolve_post_eligibility(post, tmp_path)
    assert resolved.is_v2
    assert resolved.state == "uncertain"
    assert not resolved.eligible_for_v1

    report = build_queue_report(QueueClient([post]), tmp_path)
    assert report["unprocessed_ids"] == []
    assert report["posts"][0]["state_source"] == "v2_invalid"
    assert report["posts"][0]["eligible"] is False


def test_empty_v2_projection_after_explicit_removal_is_legacy(tmp_path):
    resolved = resolve_post_eligibility(_post(meta={"_hermes_work_state": ""}), tmp_path)
    assert resolved.source == "legacy"
    assert resolved.state == "new"
    assert resolved.eligible_for_v1


def test_legacy_post_without_state_remains_explicitly_eligible(tmp_path):
    resolved = resolve_post_eligibility(_post(), tmp_path)
    assert resolved.source == "legacy"
    assert resolved.state == "new"
    assert resolved.eligible_for_v1


def test_v1_prepare_and_apply_refuse_v2_state_without_writes(tmp_path):
    post = _post(meta={"_hermes_work_state": json.dumps(WorkState().to_dict())})
    client = SingleClient(post)
    with pytest.raises(WorkflowError, match="post V2"):
        prepare_post(client, tmp_path, 1)
    with pytest.raises(WorkflowError, match="post V2"):
        apply_editorial(client, type("Config", (), {"lock_ttl": 900})(), tmp_path, 1, {})
    assert not (tmp_path / "backups" / "1").exists()


def test_ready_pending_is_idempotent_and_does_not_call_provider(tmp_path):
    post = _post(meta={"_hermes_state": "ready", "_hermes_ready_hash": "hash"})
    client = SingleClient(post)
    result = apply_editorial(client, type("Config", (), {"lock_ttl": 900})(), tmp_path, 1, {})
    assert result["status"] == "noop"
    assert result["idempotent"] is True
    assert result["wordpress_changed"] is False


def test_safe_v2_migration_preserves_checkpoint_and_releases_one_v1_candidate(tmp_path):
    state = WorkState()
    post = _post(meta={"_hermes_work_state": json.dumps(state.to_dict())})
    client = SingleClient(post)

    preview = migrate_safe_v2_to_v1(client, tmp_path, post_ids=[1])
    assert preview["post_ids"] == [1]
    assert preview["read_only"] is True
    assert post["meta"].get("_hermes_work_state")

    applied = migrate_safe_v2_to_v1(client, tmp_path, post_ids=[1], apply=True)
    assert applied["migrated"] == 1
    assert applied["writes"] == {"wordpress_metadata": 1, "content": 0, "attachments": 0}
    assert "_hermes_work_state" not in post["meta"]
    assert post["meta"]["_hermes_state"] == "new"
    assert resolve_post_eligibility(post, tmp_path).eligible_for_v1
    archive = next((tmp_path / "work" / "v1-migrations").glob("*/post.json"))
    archived = json.loads(archive.read_text(encoding="utf-8"))
    assert archived["v2_state"]["version"] == 2

    repeated = migrate_safe_v2_to_v1(client, tmp_path, post_ids=[1], apply=True)
    assert repeated["migrated"] == 0
    assert repeated["post_ids"] == []


def test_safe_v2_migration_blocks_media_effects(tmp_path):
    from unicornio_editor.pipeline_v2.model import BlockerCode, LifecycleState, MediaProgress, Phase, WorkState

    state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        relevance_approved=True,
        media=MediaProgress(required=1),
    )
    post = _post(meta={"_hermes_work_state": json.dumps(state.to_dict())})
    report = migrate_safe_v2_to_v1(SingleClient(post), tmp_path, post_ids=[1])
    assert report["candidates"] == 0
    assert report["items"] == []
