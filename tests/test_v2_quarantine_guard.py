import json
from pathlib import Path
from unittest.mock import patch

import pytest

import unicornio_editor.workflow as workflow_module
from unicornio_editor.config import Config
from unicornio_editor.batch import prepare_batch
from unicornio_editor.cli import _apply_editorial_batch, _resolve_media_batch
from unicornio_editor.pipeline_v2.media_recovery import recover_historical_attachment
from unicornio_editor.pipeline_v2.publication import (
    reconcile_published_v2,
    repair_lost_ready_hash,
)
from unicornio_editor.pipeline_v2.quarantine import (
    PostQuarantinedError,
    QuarantinePolicyError,
    assert_post_not_quarantined,
)
from unicornio_editor.pipeline_v2.visual_media import reconcile_visual_media
from unicornio_editor.workflow import (
    apply_editorial,
    build_cards,
    build_queue_report,
    discard_post,
    mark_uncertain,
    migrate_legacy_state,
    publish_post,
    retry_post,
)


POST_ID = 115152


def _quarantine(root: Path, post_id: int = POST_ID) -> None:
    path = root / "config" / "v2-quarantine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1,
        "posts": [{
            "post_id": post_id,
            "reason_code": "human_reentry_unverified",
            "reason": "revisão humana pendente",
        }],
        "release_history": [],
    }), encoding="utf-8")


class NoWriteClient:
    def __init__(self):
        self.calls = []

    def get_post(self, post_id):
        self.calls.append(("get_post", post_id))
        return {"id": post_id, "status": "pending", "meta": {}}

    def update_post(self, post_id, payload):
        self.calls.append(("update_post", post_id))

    def publish(self, post_id, **kwargs):
        self.calls.append(("publish", post_id))


def test_apply_and_publish_stop_before_wordpress_or_local_side_effects(tmp_path):
    _quarantine(tmp_path)
    client = NoWriteClient()
    config = Config("wordpress", "https://wp.test", "/wp-json/wp/v2", dry_run=False)

    with pytest.raises(PostQuarantinedError, match="115152.*apply"):
        apply_editorial(client, config, tmp_path, POST_ID, {})
    with pytest.raises(PostQuarantinedError, match="115152.*publish"):
        publish_post(client, config, tmp_path, POST_ID)
    with pytest.raises(PostQuarantinedError, match="115152.*retry"):
        retry_post(client, config, tmp_path, POST_ID)
    with pytest.raises(PostQuarantinedError, match="115152.*discard"):
        discard_post(client, config, tmp_path, POST_ID)
    with pytest.raises(PostQuarantinedError, match="115152.*mark_uncertain"):
        mark_uncertain(client, config, tmp_path, POST_ID, "revisão")

    assert client.calls == []
    assert not (tmp_path / "backups" / str(POST_ID)).exists()


def test_legacy_cron_batch_paths_guard_before_reads_snapshots_or_media_search(tmp_path):
    _quarantine(tmp_path)
    client = NoWriteClient()
    config = Config("wordpress", "https://wp.test", "/wp-json/wp/v2", dry_run=False)

    prepared = prepare_batch(client, config, tmp_path, [POST_ID], batch_id="quarantine-test")
    assert prepared["items"][0]["status"] == "quarantined"
    assert client.calls == []
    assert not (tmp_path / "backups" / str(POST_ID)).exists()

    applied = _apply_editorial_batch(
        client, config, tmp_path,
        {"schema_version": 1, "batch_id": "quarantine-apply", "items": [
            {"post_id": POST_ID, "status": "needs_retry"},
        ]},
    )
    assert applied["quarantined"] == 1
    assert client.calls == []

    with patch("unicornio_editor.media.search.search_web_images_batch") as search:
        with pytest.raises(PostQuarantinedError):
            _resolve_media_batch(
                client, config, tmp_path,
                {"posts": [{"post_id": POST_ID}]},
            )
        search.assert_not_called()


def test_migrate_state_apply_preflights_quarantined_legacy_posts(tmp_path):
    _quarantine(tmp_path)

    class LegacyClient(NoWriteClient):
        def list_pending(self, *, status, per_page, page):
            if status == "pending" and page == 1:
                return [{"id": POST_ID, "status": "pending", "meta": {}}]
            return []

    client = LegacyClient()
    config = Config("wordpress", "https://wp.test", "/wp-json/wp/v2", dry_run=False)

    with pytest.raises(PostQuarantinedError, match="115152.*migrate-state"):
        migrate_legacy_state(client, config, tmp_path, apply=True)

    assert not [call for call in client.calls if call[0] in {"update_post", "publish"}]


def test_migrate_state_rechecks_quarantine_at_the_legacy_write_boundary(tmp_path, monkeypatch):
    class LegacyClient(NoWriteClient):
        def list_pending(self, *, status, per_page, page):
            if status == "pending" and page == 1:
                return [{"id": POST_ID, "status": "pending", "meta": {}}]
            return []

    client = LegacyClient()
    config = Config("wordpress", "https://wp.test", "/wp-json/wp/v2", dry_run=False)
    original_assert = workflow_module.assert_post_not_quarantined
    calls = 0

    def quarantine_after_preflight(root, post_id, *, operation):
        nonlocal calls
        original_assert(root, post_id, operation=operation)
        calls += 1
        if calls == 1:
            _quarantine(root, post_id)

    monkeypatch.setattr(workflow_module, "assert_post_not_quarantined", quarantine_after_preflight)

    with pytest.raises(PostQuarantinedError, match="115152.*state_marker_write"):
        migrate_legacy_state(client, config, tmp_path, apply=True)

    assert not [call for call in client.calls if call[0] in {"update_post", "publish"}]


def test_quarantine_policy_missing_or_invalid_fails_closed(tmp_path):
    missing_root = tmp_path / "missing-policy"
    missing_root.mkdir()
    with pytest.raises(QuarantinePolicyError):
        assert_post_not_quarantined(missing_root, 42, operation="retry")

    path = tmp_path / "config" / "v2-quarantine.json"
    path.write_text('{"version": 1, "posts": "wrong", "release_history": []}', encoding="utf-8")
    with pytest.raises(QuarantinePolicyError):
        assert_post_not_quarantined(tmp_path, 42, operation="retry")

    for payload in (
        {"version": True, "posts": [], "release_history": []},
        {"version": 1, "posts": [{
            "post_id": "115152", "reason_code": "hold", "reason": "review",
        }], "release_history": []},
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(QuarantinePolicyError):
            assert_post_not_quarantined(tmp_path, 42, operation="retry")


def test_quarantined_post_is_not_offered_in_queue_or_cards(tmp_path):
    _quarantine(tmp_path)

    class Client:
        def list_pending(self, **kwargs):
            return [{
                "id": POST_ID,
                "status": "pending",
                "date": "2026-10-10T12:00:00",
                "content": {"raw": "texto"},
                "meta": {},
            }]

    config = Config("wordpress", "https://wp.test", "/wp-json/wp/v2")
    assert build_queue_report(Client(), tmp_path)["posts"] == []
    assert build_cards(Client(), config, tmp_path)["cards"] == []


def test_v2_recovery_and_reconciliation_writes_are_guarded(tmp_path):
    _quarantine(tmp_path)
    config = Config("wordpress", "https://wp.test", "/wp-json/wp/v2", dry_run=False)

    with pytest.raises(PostQuarantinedError):
        recover_historical_attachment(
            object(), config, tmp_path, post_id=POST_ID, media_id=1,
            evidence_path=tmp_path / "evidence.json", apply=True,
        )
    with pytest.raises(PostQuarantinedError):
        reconcile_visual_media(object(), config, tmp_path, POST_ID, apply=True)
    with pytest.raises(PostQuarantinedError):
        repair_lost_ready_hash(object(), config, tmp_path, [POST_ID], apply=True)
    with pytest.raises(PostQuarantinedError):
        reconcile_published_v2(object(), config, tmp_path, [POST_ID], apply=True)
