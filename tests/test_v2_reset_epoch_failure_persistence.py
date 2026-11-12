import json
from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2.reset_epoch import ResetEpochError, reset_epoch

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


def test_resume_preserves_existing_archive_after_registry_projection_failure(tmp_path, monkeypatch):
    import importlib

    reset_module = importlib.import_module("unicornio_editor.pipeline_v2.reset_epoch")
    posts = [_post(12)]
    _root(tmp_path, posts)
    client = ApplyClient(posts)
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")
    evidence_path = Path(preview["preview"])
    evidence = json.loads(evidence_path.read_text())
    archive = tmp_path / "work" / "v2-epochs" / "archives" / evidence["old_epoch_id"] / evidence["operation_id"]

    projection = reset_module._registry_projection

    def fail_projection(*args, **kwargs):
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(reset_module, "_registry_projection", fail_projection)
    with pytest.raises(RuntimeError, match="injected projection failure"):
        reset_epoch(
            client, object(), tmp_path, preview=False, apply=True,
            preview_evidence=evidence_path, operator_authorization="auth-test",
            report_dir=tmp_path / "apply1",
        )

    archived_state = archive / "active-v2-cycle" / "state.json"
    archived_bytes = archived_state.read_bytes()
    monkeypatch.setattr(reset_module, "_registry_projection", projection)

    result = reset_epoch(
        client, object(), tmp_path, preview=False, apply=True,
        preview_evidence=evidence_path, operator_authorization="auth-test",
        report_dir=tmp_path / "apply2",
    )

    assert result["status"] == "EDITORIAL_RESET_COMPLETE"
    assert archived_state.read_bytes() == archived_bytes


def test_resume_refuses_tampered_archive_without_deleting_it(tmp_path, monkeypatch):
    import importlib

    reset_module = importlib.import_module("unicornio_editor.pipeline_v2.reset_epoch")
    posts = [_post(12)]
    _root(tmp_path, posts)
    client = ApplyClient(posts)
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")
    evidence_path = Path(preview["preview"])
    evidence = json.loads(evidence_path.read_text())
    archive = tmp_path / "work" / "v2-epochs" / "archives" / evidence["old_epoch_id"] / evidence["operation_id"]
    projection = reset_module._registry_projection

    def fail_projection(*args, **kwargs):
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(reset_module, "_registry_projection", fail_projection)
    with pytest.raises(RuntimeError, match="injected projection failure"):
        reset_epoch(
            client, object(), tmp_path, preview=False, apply=True,
            preview_evidence=evidence_path, operator_authorization="auth-test",
            report_dir=tmp_path / "apply1",
        )

    archived_state = archive / "active-v2-cycle" / "state.json"
    archived_state.write_text("tampered archive evidence", encoding="utf-8")
    monkeypatch.setattr(reset_module, "_registry_projection", projection)
    with pytest.raises(ResetEpochError, match="existing archive differs"):
        reset_epoch(
            client, object(), tmp_path, preview=False, apply=True,
            preview_evidence=evidence_path, operator_authorization="auth-test",
            report_dir=tmp_path / "apply2",
        )

    assert archived_state.read_text(encoding="utf-8") == "tampered archive evidence"
    assert (tmp_path / "work" / "v2-maintenance.lock").is_file()


def test_backup_failure_preserves_maintenance_and_transition_for_same_operation(tmp_path, monkeypatch):
    import importlib

    reset_module = importlib.import_module("unicornio_editor.pipeline_v2.reset_epoch")
    posts = [_post(12)]
    _root(tmp_path, posts)
    client = ApplyClient(posts)
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")
    evidence_path = Path(preview["preview"])
    evidence = json.loads(evidence_path.read_text())
    transition_path = tmp_path / "work" / "v2-epochs" / "transitions" / f"{evidence['operation_id']}.json"
    archive_active = reset_module._archive_active_artifacts

    def fail_backup(*args, **kwargs):
        raise RuntimeError("injected backup failure")

    monkeypatch.setattr(reset_module, "_archive_active_artifacts", fail_backup)
    with pytest.raises(RuntimeError, match="injected backup failure"):
        reset_epoch(
            client, object(), tmp_path, preview=False, apply=True,
            preview_evidence=evidence_path, operator_authorization="auth-test",
            report_dir=tmp_path / "apply1",
        )

    assert (tmp_path / "work" / "v2-maintenance.lock").is_file()
    assert json.loads(transition_path.read_text())["operation_id"] == evidence["operation_id"]
    assert client.writes == []
    monkeypatch.setattr(reset_module, "_archive_active_artifacts", archive_active)

    result = reset_epoch(
        client, object(), tmp_path, preview=False, apply=True,
        preview_evidence=evidence_path, operator_authorization="auth-test",
        report_dir=tmp_path / "apply2",
    )

    assert result["status"] == "EDITORIAL_RESET_COMPLETE"


def test_archive_copy_does_not_overwrite_existing_mismatched_evidence(tmp_path):
    import importlib

    reset_module = importlib.import_module("unicornio_editor.pipeline_v2.reset_epoch")
    source = tmp_path / "source"
    target = tmp_path / "archive"
    source.mkdir()
    target.mkdir()
    (source / "state.json").write_text("current source", encoding="utf-8")
    archived = target / "state.json"
    archived.write_text("previous archive", encoding="utf-8")

    with pytest.raises(ResetEpochError, match="existing archive differs"):
        reset_module._copy_tree_verified(source, target)

    assert archived.read_text(encoding="utf-8") == "previous archive"


def test_resume_blocks_registry_drift_after_partial_reset(tmp_path, monkeypatch):
    import importlib
    from unicornio_editor.media.registry import connect

    reset_module = importlib.import_module("unicornio_editor.pipeline_v2.reset_epoch")
    posts = [_post(12)]
    _root(tmp_path, posts)
    client = ApplyClient(posts)
    preview = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "preview")
    evidence_path = Path(preview["preview"])
    projection = reset_module._registry_projection

    def fail_projection(*args, **kwargs):
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(reset_module, "_registry_projection", fail_projection)
    with pytest.raises(RuntimeError, match="injected projection failure"):
        reset_epoch(
            client, object(), tmp_path, preview=False, apply=True,
            preview_evidence=evidence_path, operator_authorization="auth-test",
            report_dir=tmp_path / "apply1",
        )
    monkeypatch.setattr(reset_module, "_registry_projection", projection)
    with connect(tmp_path) as db:
        db.execute(
            """INSERT INTO media_operations
            (post_id, run_id, cycle_id, cycle_round, authorization_id, phase,
             operation_status, started_at, write_started, readback_confirmed, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (999, "late-run", "late-cycle", 1, "late-auth", "relevance",
             "started", "2026-11-12T00:00:00+00:00", 0, 0, "started"),
        )

    with pytest.raises(ResetEpochError, match="Registry changed"):
        reset_epoch(
            client, object(), tmp_path, preview=False, apply=True,
            preview_evidence=evidence_path, operator_authorization="auth-test",
            report_dir=tmp_path / "apply2",
        )

    with connect(tmp_path) as db:
        assert db.execute("SELECT COUNT(*) FROM media_operations WHERE post_id=999").fetchone()[0] == 1
    assert len(client.writes) == 1
    assert (tmp_path / "work" / "v2-maintenance.lock").is_file()


def test_registry_snapshot_matches_consistent_sqlite_backup(tmp_path):
    import sqlite3
    from unicornio_editor.media.registry import connect
    from unicornio_editor.pipeline_v2.reset_epoch import _registry_snapshot

    source = tmp_path / "work" / "media_registry.sqlite"
    with connect(tmp_path) as db:
        db.execute(
            """INSERT INTO media_operations
            (post_id, run_id, cycle_id, cycle_round, authorization_id, phase,
             operation_status, started_at, write_started, readback_confirmed, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (12, "run-1", "cycle-1", 1, "auth-1", "relevance",
             "started", "2026-11-12T00:00:00+00:00", 0, 0, "started"),
        )

    backup = tmp_path / "registry-backup.sqlite"
    source_db = sqlite3.connect(source)
    backup_db = sqlite3.connect(backup)
    try:
        source_db.backup(backup_db)
    finally:
        backup_db.close()
        source_db.close()

    assert _registry_snapshot(source)[0] == _registry_snapshot(backup)[0]
