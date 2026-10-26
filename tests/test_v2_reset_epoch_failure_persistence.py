import json
from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2.reset_epoch import reset_epoch

from test_v2_reset_epoch_apply import ApplyClient, _post, _root


@pytest.mark.parametrize("failed_post", [12, 13, 14])
def test_maintenance_persists_after_first_second_and_last_post_failure(tmp_path, failed_post):
    posts = [_post(12), _post(13), _post(14)]
    _root(tmp_path, posts)
    client = ApplyClient(posts, fail_on=failed_post)
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")
    with pytest.raises(RuntimeError, match="injected"):
        reset_epoch(client, object(), tmp_path, preview=False, apply=True, preview_evidence=Path(preview["preview"]), operator_authorization="auth-test", report_dir=tmp_path / "apply1")
    assert (tmp_path / "work" / "v2-maintenance.lock").is_file()
    client.fail_on = None
    result = reset_epoch(client, object(), tmp_path, preview=False, apply=True, preview_evidence=Path(preview["preview"]), operator_authorization="auth-test", report_dir=tmp_path / "apply2")
    assert result["status"] == "EDITORIAL_RESET_COMPLETE"
    assert not (tmp_path / "work" / "v2-maintenance.lock").exists()
    for pid in (12, 13, 14):
        assert [post_id for post_id, _ in client.writes].count(pid) == 1
