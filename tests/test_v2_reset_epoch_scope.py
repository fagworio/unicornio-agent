import json
from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2.reset_epoch import ResetEpochError, reset_epoch

from test_v2_reset_epoch_apply import ApplyClient, _post, _root


def test_resume_does_not_expand_frozen_scope(tmp_path):
    posts = [_post(12), _post(13)]
    _root(tmp_path, posts)
    client = ApplyClient(posts, fail_on=13)
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")
    with pytest.raises(RuntimeError):
        reset_epoch(client, object(), tmp_path, preview=False, apply=True, preview_evidence=Path(preview["preview"]), operator_authorization="auth-test", report_dir=tmp_path / "apply1")
    client.posts[99] = _post(99)
    client.fail_on = None
    result = reset_epoch(client, object(), tmp_path, preview=False, apply=True, preview_evidence=Path(preview["preview"]), operator_authorization="auth-test", report_dir=tmp_path / "apply2")
    assert result["reset_post_ids"] == [12, 13]
    assert all(post_id != 99 for post_id, _ in client.writes)


def test_resume_rejects_authorization_identity_change(tmp_path):
    posts = [_post(12), _post(13)]
    _root(tmp_path, posts)
    client = ApplyClient(posts, fail_on=13)
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")
    with pytest.raises(RuntimeError):
        reset_epoch(client, object(), tmp_path, preview=False, apply=True, preview_evidence=Path(preview["preview"]), operator_authorization="auth-test", report_dir=tmp_path / "apply1")
    with pytest.raises(ResetEpochError, match="authorization identity mismatch"):
        reset_epoch(client, object(), tmp_path, preview=False, apply=True, preview_evidence=Path(preview["preview"]), operator_authorization="other-auth", report_dir=tmp_path / "apply2")
    assert (tmp_path / "work" / "v2-maintenance.lock").is_file()
