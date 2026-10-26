import json
from types import SimpleNamespace

import pytest

from unicornio_editor.pipeline_v2.epoch_protection import EpochProtectionError
from unicornio_editor.pipeline_v2.model import BlockerCode, LifecycleState, Phase, WorkState
from unicornio_editor.pipeline_v2.reset_epoch import _human_required_audit
from unicornio_editor.pipeline_v2.runtime import WordPressWriterV2
from unicornio_editor.workflow import _publish_post_unlocked


class NoWriteClient:
    def __init__(self):
        self.calls = []
    def get_post(self, post_id):
        self.calls.append(("get", post_id))
        return {"id": post_id, "status": "pending", "meta": {}}
    def update_post(self, *args, **kwargs):
        self.calls.append(("update", args, kwargs))
        raise AssertionError("protected post reached WordPress write")


def _epoch(root, ids):
    path = root / "work/v2-epochs/active"
    path.mkdir(parents=True)
    (path / "epoch.json").write_text(json.dumps({"schema_version": 1, "epoch_id": "new", "protected_transaction_ids": ids}))


def test_protected_writer_is_blocked_before_any_effect(tmp_path):
    _epoch(tmp_path, [115757])
    client = NoWriteClient()
    state = WorkState(state=LifecycleState.PENDING, phase=Phase.RELEVANCE)
    with pytest.raises(EpochProtectionError):
        WordPressWriterV2(client, tmp_path).commit(115757, {"post": {}}, state, SimpleNamespace(type=SimpleNamespace(value="retry")))
    assert client.calls == []


def test_protected_publisher_is_blocked(tmp_path):
    _epoch(tmp_path, [115757])
    result = _publish_post_unlocked(NoWriteClient(), SimpleNamespace(dry_run=False), tmp_path, 115757)
    assert result["epoch_protected"] is True
    assert result["wordpress_changed"] is False


def test_pending_human_required_with_evidence_is_review_required(tmp_path):
    (tmp_path / "work/v2-journal").mkdir(parents=True)
    (tmp_path / "work/v2-journal/44.json").write_text("{}")
    state = WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING)
    post = {"id": 44, "status": "pending", "content": {"raw": "x"}, "featured_media": 0, "meta": {"_hermes_work_state": json.dumps(state.to_dict())}}
    audit = _human_required_audit(tmp_path, [post], {}, set())
    assert audit[0]["classification"] == "REVIEW_REQUIRED"
