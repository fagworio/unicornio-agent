import hashlib
import json

from unicornio_editor.pipeline_v2.model import BlockerCode, LifecycleState, Phase, WorkState
from unicornio_editor.pipeline_v2.reset_epoch import _human_required_audit


def test_human_required_audit_distinguishes_pending_mismatch(tmp_path):
    state = WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING)
    post = {"id": 44, "status": "pending", "content": {"raw": "x"}, "featured_media": 0, "meta": {"_hermes_work_state": json.dumps(state.to_dict())}}
    audit = _human_required_audit(tmp_path, [post], {}, set())
    assert audit[0]["post_id"] == 44
    assert audit[0]["wordpress_status"] == "pending"
    assert audit[0]["canonical_state"] == "human_required"
    assert audit[0]["classification"] == "SAFE_RESET"
    assert audit[0]["phase"] == "media"
    assert audit[0]["blocker"] == "inline_missing"
    assert audit[0]["content_sha256"] == hashlib.sha256(b"x").hexdigest()
