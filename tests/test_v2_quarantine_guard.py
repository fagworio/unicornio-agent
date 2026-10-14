import json
from pathlib import Path

import pytest

from unicornio_editor.config import Config
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


def test_quarantine_policy_missing_or_invalid_fails_closed(tmp_path):
    missing_root = tmp_path / "missing-policy"
    missing_root.mkdir()
    with pytest.raises(QuarantinePolicyError):
        assert_post_not_quarantined(missing_root, 42, operation="retry")

    path = tmp_path / "config" / "v2-quarantine.json"
    path.write_text('{"version": 1, "posts": "wrong", "release_history": []}', encoding="utf-8")
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
