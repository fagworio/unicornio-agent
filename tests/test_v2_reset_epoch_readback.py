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


@pytest.mark.parametrize("changed_field", ["content", "featured_media"])
def test_reset_rejects_readback_that_changes_content_or_image(tmp_path, changed_field):
    posts = [_post(12)]
    posts[0]["featured_media"] = 77
    _root(tmp_path, posts)
    client = ApplyClient(posts)
    original_update = client.update_post

    def update_then_mutate(post_id, payload):
        result = original_update(post_id, payload)
        if changed_field == "content":
            client.posts[post_id]["content"]["raw"] = "unexpected content mutation"
        else:
            client.posts[post_id]["featured_media"] = 999
        return result

    client.update_post = update_then_mutate
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")

    with pytest.raises(ResetEpochError, match="content or featured media changed"):
        reset_epoch(
            client, object(), tmp_path, preview=False, apply=True,
            preview_evidence=Path(preview["preview"]), operator_authorization="auth-test",
            report_dir=tmp_path / "apply",
        )

    assert len(client.writes) == 1
    assert (tmp_path / "work" / "v2-maintenance.lock").is_file()
