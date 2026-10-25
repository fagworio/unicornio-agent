import json
import sqlite3
from pathlib import Path

from unicornio_editor.pipeline_v2.runtime import (
    _incomplete_v2_journal_conflicts,
    _media_effect_evidence,
    _record_v2_writer_exception,
    _verify_acquisition_checkpoint_for_resume,
    _write_v2_journal_checkpoint,
    _write_v2_media_checkpoint,
    recover_interrupted_journal,
)
from unicornio_editor.pipeline_v2.model import (
    FeaturedProgress,
    InlineMedia,
    LifecycleState,
    MediaProgress,
    Phase,
    WorkState,
)
from unicornio_editor.locking import LockManager


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


def _media_resume_registry(root: Path, *, write_started: int = 0) -> None:
    path = root / "work" / "media_registry.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE media_runs (run_id TEXT, post_id INTEGER, planned_deficits TEXT,
                acquisition_outcome TEXT, created_at TEXT);
            CREATE TABLE media_decisions (run_id TEXT, post_id INTEGER, candidate_id TEXT,
                disposition TEXT, reason_code TEXT, scope TEXT, created_at TEXT);
            CREATE TABLE media_usage (post_id INTEGER, wordpress_media_id INTEGER,
                status TEXT, applied_at TEXT, run_id TEXT, cycle_id TEXT,
                cycle_round INTEGER, authorization_id TEXT, accepted_at TEXT,
                readback_confirmed_at TEXT);
            CREATE TABLE media_operations (post_id INTEGER, run_id TEXT, cycle_id TEXT,
                cycle_round INTEGER, authorization_id TEXT, phase TEXT,
                operation_status TEXT, started_at TEXT, completed_at TEXT,
                write_started INTEGER, readback_confirmed INTEGER, outcome TEXT);
            """
        )
        db.execute(
            "INSERT INTO media_runs(run_id, post_id, planned_deficits) VALUES (?, ?, ?)",
            ("run-media", 115757, "{}"),
        )
        db.execute(
            """INSERT INTO media_operations
               (post_id, run_id, cycle_id, cycle_round, authorization_id,
                operation_status, started_at, write_started, readback_confirmed,
                completed_at, outcome)
               VALUES (?, ?, ?, ?, ?, 'started', '2026-10-24T10:00:00+00:00', ?, 0, NULL, '')""",
            (115757, "run-media", "cycle-a", 4, "auth-a", write_started),
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


def test_recovery_waits_for_shared_post_lock_and_preserves_journal_when_busy(tmp_path):
    _empty_media_registry(tmp_path)
    path = _journal(tmp_path, {
        "status": "running", "post_id": 115676, "run_id": "run-a",
        "cycle_id": "cycle-a", "cycle_round": 1,
        "authorization_id": "auth-a", "protocol_version": 2,
        "writer_started": False, "state": {"state": "pending", "phase": "media"},
    })
    held = LockManager(tmp_path / "work" / "locks").acquire(115676)
    held.__enter__()
    try:
        result = recover_interrupted_journal(tmp_path, 115676, apply=True)
        assert result["recovered"] is False
        assert result["locked"] is True
        assert result["reason"] == "post_lock_unavailable"
        assert json.loads(path.read_text(encoding="utf-8"))["status"] == "running"
    finally:
        held.__exit__(None, None, None)

    resumed = recover_interrupted_journal(tmp_path, 115676, apply=True)
    assert resumed["recovered"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "interrupted_pre_writer"


def test_recovery_rechecks_expected_journal_identity_under_post_lock(tmp_path):
    _empty_media_registry(tmp_path)
    path = _journal(tmp_path, {
        "status": "running", "post_id": 115676, "run_id": "run-current",
        "cycle_id": "cycle-current", "cycle_round": 3,
        "authorization_id": "auth-current", "protocol_version": 2,
        "writer_started": False, "state": {"state": "pending", "phase": "media"},
    })

    result = recover_interrupted_journal(
        tmp_path,
        115676,
        apply=True,
        expected_identity={
            "run_id": "run-old",
            "cycle_id": "cycle-current",
            "authorization_id": "auth-current",
        },
    )

    assert result["recovered"] is False
    assert result["reason"] == "journal_identity_mismatch"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "running"


def test_recover_validated_protocol_journal_only_with_pre_writer_evidence(tmp_path):
    _empty_media_registry(tmp_path)
    path = _journal(tmp_path, {
        "status": "validated", "post_id": 115676, "run_id": "run-validated",
        "cycle_id": "cycle-a", "cycle_round": 2,
        "authorization_id": "auth-a", "protocol_version": 2,
        "writer_started": False, "state": {"state": "pending", "phase": "media"},
    })

    result = recover_interrupted_journal(tmp_path, 115676, apply=True)

    assert result["recovered"] is True
    assert json.loads(path.read_text())["status"] == "interrupted_pre_writer"


def test_recover_validated_journal_with_writer_intent_remains_blocked(tmp_path):
    _empty_media_registry(tmp_path)
    path = _journal(tmp_path, {
        "status": "validated", "post_id": 115676, "run_id": "run-validated",
        "protocol_version": 2, "writer_started": True,
        "state": {"state": "pending", "phase": "media"},
    })

    result = recover_interrupted_journal(tmp_path, 115676, apply=True)

    assert result["recovered"] is False
    assert result["reason"] == "insufficient_protocol_evidence"
    assert json.loads(path.read_text())["status"] == "validated"


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


def test_correlated_pre_writer_media_checkpoint_resumes_only_after_attachment_readback(tmp_path):
    base_state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        relevance_approved=True,
    )
    checkpoint_media = MediaProgress(
        required=1,
        inline=(InlineMedia(115775, "https://wp.test/uploads/115775.webp", 0),),
        featured=FeaturedProgress(),
    )
    path = tmp_path / "work" / "v2-journal" / "115757.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "running", "post_id": 115757, "run_id": "run-media",
        "cycle_id": "cycle-a", "cycle_round": 4,
        "authorization_id": "auth-a", "protocol_version": 2,
        "writer_started": False, "readback": False,
        "state": base_state.to_dict(),
    }), encoding="utf-8")
    _write_v2_media_checkpoint(
        tmp_path, 115757, checkpoint_media, run_id="run-media",
        cycle_id="cycle-a", cycle_round=4, authorization_id="auth-a",
    )
    _media_resume_registry(tmp_path)

    evidence = _media_effect_evidence(tmp_path, 115757, json.loads(path.read_text()))
    assert evidence["evidence_status"] == "acquisition_checkpoint_correlated"
    assert evidence["acquisition_checkpoint"]["media_ids"] == [115775]
    assert evidence["acquisition_checkpoint"]["new_media_ids"] == [115775]

    class Client:
        def get_post(self, post_id):
            return {
                "id": post_id,
                "status": "pending",
                "meta": {"_hermes_work_state": json.dumps(base_state.to_dict())},
            }

        def get_media(self, media_id):
            return {"id": media_id, "source_url": "https://wp.test/uploads/115775.webp"}

    context = {"v2_state": WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        relevance_approved=True,
        media=checkpoint_media,
    )}
    verification = _verify_acquisition_checkpoint_for_resume(
        Client(), tmp_path, 115757, json.loads(path.read_text()), context
    )
    assert verification is not None
    assert verification["wordpress_readback_media_ids"] == [115775]

    # If the acquired media is already attached to the post content, the
    # checkpoint no longer proves an acquisition-only pre-writer state.
    post = Client().get_post(115757)
    post["content"] = {
        "raw": '<p>Body</p><img src="https://wp.test/uploads/115775.webp">'
    }

    class ClientWithPost(Client):
        def get_post(self, requested_id):
            return post if requested_id == 115757 else None

    assert _verify_acquisition_checkpoint_for_resume(
        ClientWithPost(), tmp_path, 115757, json.loads(path.read_text()), context
    ) is None

    post["content"] = {"raw": "<p>Article body without acquired image.</p>"}
    verification = _verify_acquisition_checkpoint_for_resume(
        ClientWithPost(), tmp_path, 115757, json.loads(path.read_text()), context
    )
    assert verification is not None

    recovered = recover_interrupted_journal(
        tmp_path,
        115757,
        apply=True,
        expected_identity={
            "run_id": "run-media",
            "cycle_id": "cycle-a",
            "cycle_round": "4",
            "authorization_id": "auth-a",
        },
        media_checkpoint_resume=verification,
    )
    saved = json.loads(path.read_text())
    assert recovered["recovered"] is True
    assert recovered["recovery_classification"] == "ACCEPTED_MEDIA_CHECKPOINT_RESUMABLE"
    assert saved["status"] == "interrupted_pre_writer"
    assert saved["writer_started"] is False
    assert saved["recovery_evidence"]["media_ids"] == [115775]
    assert _incomplete_v2_journal_conflicts(
        tmp_path, [115757], resumable_media_checkpoint_ids={115757: {115775}}
    ) == []


def test_pre_writer_media_checkpoint_stays_blocked_when_attachment_readback_fails(tmp_path):
    base_state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        relevance_approved=True,
    )
    checkpoint_media = MediaProgress(
        required=1,
        inline=(InlineMedia(115775, "https://wp.test/uploads/115775.webp", 0),),
    )
    path = tmp_path / "work" / "v2-journal" / "115757.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "running", "post_id": 115757, "run_id": "run-media",
        "cycle_id": "cycle-a", "cycle_round": 4,
        "authorization_id": "auth-a", "protocol_version": 2,
        "writer_started": False, "readback": False,
        "state": base_state.to_dict(),
    }), encoding="utf-8")
    _write_v2_media_checkpoint(
        tmp_path, 115757, checkpoint_media, run_id="run-media",
        cycle_id="cycle-a", cycle_round=4, authorization_id="auth-a",
    )
    _media_resume_registry(tmp_path)

    class Client:
        def get_post(self, post_id):
            return {
                "id": post_id,
                "status": "pending",
                "meta": {"_hermes_work_state": json.dumps(base_state.to_dict())},
            }

        def get_media(self, media_id):
            return {"id": media_id, "source_url": "https://wp.test/uploads/other.webp"}

    verification = _verify_acquisition_checkpoint_for_resume(
        Client(),
        tmp_path,
        115757,
        json.loads(path.read_text()),
        {"v2_state": WorkState(
            state=LifecycleState.PENDING,
            phase=Phase.MEDIA,
            relevance_approved=True,
            media=checkpoint_media,
        )},
    )
    assert verification is None
    recovered = recover_interrupted_journal(tmp_path, 115757, apply=True)
    assert recovered["recovered"] is False
    assert recovered["reason"] == "media_effects_pre_writer"
    assert json.loads(path.read_text())["status"] == "running"


def test_media_checkpoint_correlation_rejects_registry_writer_activity(tmp_path):
    state = WorkState(state=LifecycleState.PENDING, phase=Phase.MEDIA).to_dict()
    path = tmp_path / "work" / "v2-journal" / "115757.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "running", "post_id": 115757, "run_id": "run-media",
        "cycle_id": "cycle-a", "cycle_round": 4,
        "authorization_id": "auth-a", "protocol_version": 2,
        "writer_started": False, "readback": False, "state": state,
    }), encoding="utf-8")
    _write_v2_media_checkpoint(
        tmp_path,
        115757,
        MediaProgress(
            required=1,
            inline=(InlineMedia(115775, "https://wp.test/uploads/115775.webp", 0),),
        ),
        run_id="run-media",
        cycle_id="cycle-a",
        cycle_round=4,
        authorization_id="auth-a",
    )
    _media_resume_registry(tmp_path, write_started=1)

    evidence = _media_effect_evidence(tmp_path, 115757, json.loads(path.read_text()))
    assert evidence["evidence_status"] != "acquisition_checkpoint_correlated"
    recovered = recover_interrupted_journal(tmp_path, 115757, apply=True)
    assert recovered["recovered"] is False
    assert recovered["reason"] == "media_effects_pre_writer"
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


def test_outer_writer_exception_preserves_detailed_media_readback_journal(tmp_path):
    path = _journal(tmp_path, {
        "status": "media_readback_failed", "post_id": 115676, "run_id": "run-a",
        "protocol_version": 2, "writer_started": True,
        "media_readback_status": "failed",
        "media_readback_failure": {"field": "featured_media", "expected": 9, "actual": 0},
        "apply_media_records": [{"media_id": 9, "destination": "apply_readback_failed"}],
        "state": WorkState().to_dict(),
    })

    _record_v2_writer_exception(
        tmp_path, 115676, WorkState(), run_id="run-a", error=RuntimeError("outer failure")
    )

    saved = json.loads(path.read_text())
    assert saved["status"] == "media_readback_failed"
    assert saved["media_readback_status"] == "failed"
    assert saved["media_readback_failure"]["field"] == "featured_media"
    assert saved["apply_media_records"][0]["media_id"] == 9
    assert saved["writer_exception"] == {
        "error_class": "RuntimeError", "detail": "outer failure"
    }
