from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2.reset_epoch import ResetEpochError, reset_epoch

from test_v2_reset_epoch_apply import ApplyClient, _post, _root


def test_resume_stops_on_completed_post_readback_divergence(tmp_path):
    posts = [_post(12), _post(13)]
    _root(tmp_path, posts)
    client = ApplyClient(posts, fail_on=13)
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")
    with pytest.raises(RuntimeError):
        reset_epoch(client, object(), tmp_path, preview=False, apply=True, preview_evidence=Path(preview["preview"]), operator_authorization="auth-test", report_dir=tmp_path / "apply1")
    client.posts[12]["content"]["raw"] = "changed outside reset"
    client.fail_on = None
    with pytest.raises(ResetEpochError, match="completed readback diverged"):
        reset_epoch(client, object(), tmp_path, preview=False, apply=True, preview_evidence=Path(preview["preview"]), operator_authorization="auth-test", report_dir=tmp_path / "apply2")
    assert (tmp_path / "work" / "v2-maintenance.lock").is_file()
