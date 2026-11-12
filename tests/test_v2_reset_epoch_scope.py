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


def test_preview_hash_binds_per_post_journal_bytes(tmp_path):
    posts = [_post(12)]
    _root(tmp_path, posts)
    client = ApplyClient(posts)
    journal = tmp_path / "work" / "v2-journal" / "12.json"
    journal.write_text(json.dumps({"phase": "committed", "readback": True}), encoding="utf-8")

    first = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview1")
    journal.write_text(json.dumps({"phase": "committing", "readback": False}), encoding="utf-8")
    second = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview2")

    assert first["snapshot_hash"] != second["snapshot_hash"]


def test_apply_revalidates_journal_after_acquiring_maintenance_locks(tmp_path, monkeypatch):
    posts = [_post(12)]
    _root(tmp_path, posts)
    client = ApplyClient(posts)
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")

    from unicornio_editor.pipeline_v2.lock import RunSessionLock

    acquire = RunSessionLock.acquire
    changed = False

    def acquire_and_change_journal(lock):
        nonlocal changed
        acquired = acquire(lock)
        if acquired and lock.path.name == "v2-reset-epoch.lock" and not changed:
            journal = tmp_path / "work" / "v2-journal" / "12.json"
            journal.write_text(json.dumps({"writer_started": True}), encoding="utf-8")
            changed = True
        return acquired

    monkeypatch.setattr(RunSessionLock, "acquire", acquire_and_change_journal)

    with pytest.raises(ResetEpochError, match="snapshot"):
        reset_epoch(
            client,
            object(),
            tmp_path,
            preview=False,
            apply=True,
            preview_evidence=Path(preview["preview"]),
            operator_authorization="auth-test",
            report_dir=tmp_path / "apply",
        )

    assert client.writes == []
    assert (tmp_path / "work" / "v2-maintenance.lock").exists() is False


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
