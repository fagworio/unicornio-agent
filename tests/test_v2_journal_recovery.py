import json
import sqlite3
from pathlib import Path

from unicornio_editor.pipeline_v2.runtime import (
    _write_v2_journal_checkpoint,
    recover_interrupted_journal,
)
from unicornio_editor.pipeline_v2.model import WorkState


def _journal(root: Path, payload: dict) -> Path:
    path = root / "work" / "v2-journal" / "115676.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _empty_media_registry(root: Path) -> None:
    path = root / "work" / "media_registry.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE media_runs (
                run_id TEXT, post_id INTEGER, planned_deficits TEXT,
                acquisition_outcome TEXT, created_at TEXT
            );
            CREATE TABLE media_decisions (
                run_id TEXT, post_id INTEGER, candidate_id TEXT,
                disposition TEXT, reason_code TEXT, scope TEXT, created_at TEXT
            );
            CREATE TABLE media_usage (
                post_id INTEGER, wordpress_media_id INTEGER,
                status TEXT, applied_at TEXT
            );
            """
        )


def test_recover_protocol_running_journal_as_pre_writer_interruption(tmp_path):
    _empty_media_registry(tmp_path)
    path = _journal(tmp_path, {
        "status": "running", "post_id": 115676, "run_id": "run-a",
        "cycle_id": "cycle-a", "cycle_round": 1,
        "authorization_id": "auth-a", "protocol_version": 2,
        "writer_started": False, "state": {"state": "pending", "phase": "media"},
    })
    result = recover_interrupted_journal(tmp_path, 115676, apply=True)
    saved = json.loads(path.read_text())
    assert result["recovered"] is True
    assert result["write_started"] is False
    assert saved["status"] == "interrupted_pre_writer"
    assert saved["resumable"] is True
    assert saved["failure_class"] == "process_interrupted"
    assert saved["recovered_from_run_id"] == "run-a"


def test_recovery_refuses_ambiguous_running_journal(tmp_path):
    _empty_media_registry(tmp_path)
    path = _journal(tmp_path, {
        "status": "running", "post_id": 115676, "run_id": "run-a",
        "state": {"state": "pending", "phase": "media"},
    })
    result = recover_interrupted_journal(tmp_path, 115676, apply=False)
    assert result["recovered"] is False
    assert result["reason"] == "insufficient_protocol_evidence"
    assert json.loads(path.read_text())["status"] == "running"


def test_recovery_blocks_when_media_registry_is_absent(tmp_path):
    path = _journal(tmp_path, {
        "status": "running", "post_id": 115676, "run_id": "run-media",
        "protocol_version": 2, "writer_started": False,
        "state": {"state": "pending", "phase": "media"},
    })

    result = recover_interrupted_journal(tmp_path, 115676, apply=False)

    assert result["recovered"] is False
    assert result["reason"] == "media_effects_pre_writer"
    assert result["media_effect_evidence"]["evidence_status"] == "evidence_unavailable"
    assert json.loads(path.read_text())["status"] == "running"


def test_recovery_blocks_when_media_registry_is_incomplete(tmp_path):
    path = _journal(tmp_path, {
        "status": "running", "post_id": 115676, "run_id": "run-media",
        "protocol_version": 2, "writer_started": False,
        "state": {"state": "pending", "phase": "media"},
    })
    registry = tmp_path / "work" / "media_registry.sqlite"
    with sqlite3.connect(registry) as db:
        db.execute(
            "CREATE TABLE media_decisions (run_id TEXT, disposition TEXT, reason_code TEXT)"
        )

    result = recover_interrupted_journal(tmp_path, 115676, apply=False)

    assert result["recovered"] is False
    assert result["reason"] == "media_effects_pre_writer"
    assert result["media_effect_evidence"]["evidence_status"] == "evidence_unavailable"
    assert json.loads(path.read_text())["status"] == "running"


def test_recovery_blocks_pre_writer_journal_with_accepted_media_effect(tmp_path):
    path = tmp_path / "work" / "v2-journal" / "115633.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "running", "post_id": 115633, "run_id": "run-media",
        "cycle_id": "cycle-a", "cycle_round": 4,
        "authorization_id": "auth-a", "protocol_version": 2,
        "writer_started": False,
        "state": {"state": "pending", "phase": "media", "media": {
            "inline": {"required": 3, "accepted": []},
            "featured": {"status": "missing"},
        }},
    }), encoding="utf-8")
    registry = tmp_path / "work" / "media_registry.sqlite"
    with sqlite3.connect(registry) as db:
        db.execute(
            """CREATE TABLE media_decisions (
                run_id TEXT, candidate_id TEXT, disposition TEXT,
                reason_code TEXT, scope TEXT
            )"""
        )
        db.execute(
            """INSERT INTO media_decisions
               (run_id, candidate_id, disposition, reason_code, scope)
               VALUES (?, ?, ?, ?, ?)""",
            ("run-media", "candidate-1", "accepted", "candidate_accepted", "candidate"),
        )

    result = recover_interrupted_journal(tmp_path, 115633, apply=True)

    assert result["recovered"] is False
    assert result["reason"] == "media_effects_pre_writer"
    assert result["recovery_classification"] == "MEDIA_EFFECTS_PRE_WRITER"
    assert "accepted_media_decision" in result["media_effect_evidence"]["reasons"]
    assert json.loads(path.read_text())["status"] == "running"


def test_legacy_recovery_requires_explicit_evidence(tmp_path):
    _empty_media_registry(tmp_path)
    path = _journal(tmp_path, {
        "status": "running", "post_id": 115676, "run_id": "run-a",
        "cycle_id": "cycle-a", "cycle_round": 1,
        "authorization_id": "auth-a", "state": {"state": "pending", "phase": "media"},
    })
    evidence = {
        "writer_not_started": True,
        "writer_events": [],
        "wordpress_status": "pending",
        "applied_usage_count": 0,
    }
    result = recover_interrupted_journal(tmp_path, 115676, apply=True, legacy_evidence=evidence)
    saved = json.loads(path.read_text())
    assert result["recovered"] is True
    assert saved["legacy_recovery_evidence"] == evidence
    assert saved["status"] == "interrupted_pre_writer"


def test_recovery_is_idempotent(tmp_path):
    _empty_media_registry(tmp_path)
    _journal(tmp_path, {
        "status": "running", "post_id": 115676, "run_id": "run-a",
        "cycle_id": "cycle-a", "cycle_round": 1,
        "authorization_id": "auth-a", "protocol_version": 2,
        "writer_started": False, "state": {"state": "pending", "phase": "media"},
    })
    first = recover_interrupted_journal(tmp_path, 115676, apply=True)
    before = (tmp_path / "work/v2-journal/115676.json").read_text()
    second = recover_interrupted_journal(tmp_path, 115676, apply=True)
    after = (tmp_path / "work/v2-journal/115676.json").read_text()
    assert first["recovered"] is True
    assert second["recovered"] is False
    assert second["reason"] == "already_recovered"
    assert before == after


def test_resumable_journal_preserves_recovery_chain_for_new_run(tmp_path):
    path = _journal(tmp_path, {
        "status": "interrupted_pre_writer", "post_id": 115676,
        "run_id": "old-run", "resumable": True,
        "failure_class": "process_interrupted", "state": WorkState().to_dict(),
    })
    _write_v2_journal_checkpoint(
        tmp_path, 115676, "running", WorkState(), run_id="new-run",
        cycle_id="cycle-a", cycle_round=2, authorization_id="auth-b",
    )
    saved = json.loads(path.read_text())
    assert saved["status"] == "running"
    assert saved["run_id"] == "new-run"
    assert saved["recovered_from_run_id"] == "old-run"
    assert saved["recovery_chain"][0]["run_id"] == "old-run"
