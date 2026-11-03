from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2 import cycle_recovery_v2 as recovery
from unicornio_editor.pipeline_v2 import transaction_admin
from unicornio_editor.pipeline_v2.lock import RunSessionLock
from unicornio_editor.pipeline_v2.cycle_runtime import (
    _release_post_lock_batch,
    _reserve_post_lock_batch,
)

CYCLE = "c" * 32
AUTH = "a" * 32
RUNS = {101: "r" * 32, 102: "s" * 32, 103: "t" * 32, 104: "u" * 32}
SELECTED = [101, 102, 103, 104, 105]


def _root(tmp_path: Path) -> Path:
    (tmp_path / "work" / "v2-cycle").mkdir(parents=True)
    (tmp_path / "work" / "v2-journal").mkdir(parents=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "work" / "v2-cycle" / "state.json").write_text(json.dumps({
        "schema_version": 3, "cycle_id": CYCLE, "status": "running", "phase": "ADMISSION",
        "pending_round": {"round_number": 3, "authorization_id": AUTH, "preview_selected_ids": SELECTED},
    }))
    (tmp_path / "work" / "v2-cycle" / "journal.jsonl").write_text("{}\n")
    (tmp_path / "config" / "v2-quarantine.json").write_text(json.dumps({
        "version": 1, "posts": [{"post_id": i, "reason_code": "legacy", "reason": "existing"} for i in range(200, 209)],
        "release_history": [],
    }))
    db = tmp_path / "work" / "media_registry.sqlite"
    with sqlite3.connect(db) as conn:
        for table in ("media_runs", "media_operations", "media_decisions", "media_usage"):
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, marker TEXT)")
        conn.commit()
    return tmp_path


def _evidence(monkeypatch):
    classes = {101: "COMMITTED_NO_REPLAY", 102: "COMMITTED_NO_REPLAY", 103: "COMMITTED_NO_REPLAY", 104: "UNCERTAIN_ISOLATED", 105: "NOT_EXECUTED"}
    def fake(*args, post_id, **kwargs):
        row = {"post_id": post_id, "classification": classes[post_id], "registry": {"present": True, "schema_complete": True}, "wordpress": {"status": "pending"}, "journal": {"present": post_id != 105, "run_id": RUNS.get(post_id, "")}}
        if post_id == 105:
            row["no_write_evidence"] = {"proof_of_absence": True}
        return row
    monkeypatch.setattr(recovery, "_round_post_evidence", fake)


def _kwargs(root: Path, **extra):
    value = dict(root=root, client=object(), cycle_id=CYCLE, authorization_id=AUTH, pending_round=3,
                 selected_ids=SELECTED, committed_post_ids=[101, 102, 103], uncertain_post_ids=[104],
                 not_executed_post_ids=[105], pre_writer_recoverable_post_ids=[], run_ids=RUNS,
                 operator_authorization="test-operator", new_cycle_id="n" * 32)
    value.update(extra)
    return value


def test_missing_transaction_classification_is_fail_closed():
    with pytest.raises(recovery.CycleRecoveryError, match="missing transaction classification"):
        recovery._class_sets([{"post_id": 101}])


def test_not_executed_without_journal_still_requires_independent_no_write_proof():
    evidence = {
        "classification": "NOT_EXECUTED",
        "journal": {"present": False},
    }

    assert recovery._not_executed_evidence_is_complete(evidence) is False
    assert recovery._not_executed_evidence_is_complete({
        **evidence,
        "no_write_evidence": {"proof_of_absence": True},
    }) is True


def test_real_five_way_partition_and_committed_posts_not_quarantined(tmp_path, monkeypatch):
    root = _root(tmp_path); _evidence(monkeypatch)
    preview = recovery.recover_blocked_cycle(**_kwargs(root))
    assert preview["round_classifications"] == {
        "COMMITTED_NO_REPLAY": [101, 102, 103], "NOT_EXECUTED": [105],
        "UNCERTAIN_ISOLATED": [104], "PRE_WRITER_RECOVERABLE": [],
    }
    result = recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))
    assert result["status"] == "applied"
    policy = json.loads((root / "config" / "v2-quarantine.json").read_text())
    assert [row["post_id"] for row in policy["posts"]] == list(range(200, 209)) + [104]
    assert all(post_id not in [101, 102, 103, 105] for post_id in [row["post_id"] for row in policy["posts"]])
    again = recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))
    assert again["status"] == "already_applied"


def test_resume_after_quarantine_write_interruption(tmp_path, monkeypatch):
    root = _root(tmp_path); _evidence(monkeypatch)
    preview = recovery.recover_blocked_cycle(**_kwargs(root))
    original = recovery.atomic_write_text
    def fail_once(path, text):
        if Path(path) == root / "config" / "v2-quarantine.json":
            monkeypatch.setattr(recovery, "atomic_write_text", original)
            raise OSError("simulated interruption")
        return original(path, text)
    monkeypatch.setattr(recovery, "atomic_write_text", fail_once)
    with pytest.raises(OSError):
        recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))
    resumed = recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))
    assert resumed["status"] == "applied"
    assert json.loads((root / "work" / "v2-cycle" / "state.json").read_text())["cycle_id"] == "n" * 32


def test_registry_unavailable_is_fail_closed(tmp_path, monkeypatch):
    root = _root(tmp_path); _evidence(monkeypatch)
    (root / "work" / "media_registry.sqlite").unlink()
    monkeypatch.setattr(recovery, "_round_post_evidence", lambda *args, post_id, **kwargs: {
        "post_id": post_id, "classification": "NOT_EXECUTED" if post_id == 105 else "COMMITTED_NO_REPLAY",
        "registry": {"present": False}, "wordpress": {"status": "pending"},
        "journal": {"present": post_id != 105, "run_id": RUNS.get(post_id, "")},
        "no_write_evidence": {"proof_of_absence": True} if post_id == 105 else None,
    })
    with pytest.raises(recovery.CycleRecoveryError, match="MediaRegistry"):
        recovery.recover_blocked_cycle(**_kwargs(root, committed_post_ids=[101, 102, 103, 104], uncertain_post_ids=[], not_executed_post_ids=[105]))


def test_identityless_terminal_journal_with_no_write_proof_is_not_executed(tmp_path, monkeypatch):
    root = _root(tmp_path)
    journal_dir = root / "work" / "v2-journal"
    (journal_dir / "105.json").write_text(json.dumps({
        "status": "committed",
        "readback": True,
    }))

    monkeypatch.setattr(transaction_admin, "_registry_snapshot", lambda *args, **kwargs: {
        "present": True,
        "schema_complete": True,
        "run": [],
        "decisions": [],
        "usages": [],
        "operations": [],
    })
    monkeypatch.setattr(transaction_admin, "_post_no_write_evidence", lambda *args, **kwargs: {
        "proof_of_absence": True,
    })

    class Client:
        def get_post(self, post_id):
            return {
                "id": post_id,
                "status": "pending",
                "meta": {},
                "content": {"raw": "body"},
                "title": {"raw": "title"},
                "featured_media": 0,
            }

    evidence = transaction_admin._round_post_evidence(
        root,
        Client(),
        post_id=105,
        cycle_id=CYCLE,
        authorization_id=AUTH,
        pending_round=3,
    )
    assert evidence["classification"] == "NOT_EXECUTED"
    assert evidence["no_write_evidence"]["proof_of_absence"] is True


def test_recovery_accepts_identityless_terminal_journal_with_no_write_proof(tmp_path, monkeypatch):
    root = _root(tmp_path)

    def fake(*args, post_id, **kwargs):
        if post_id == 105:
            return {
                "post_id": 105,
                "classification": "NOT_EXECUTED",
                "registry": {"present": True, "schema_complete": True},
                "wordpress": {"status": "pending"},
                "journal": {
                    "present": True,
                    "status": "committed",
                    "readback": True,
                    "run_id": "",
                    "cycle_id": None,
                    "cycle_round": None,
                    "authorization_id": None,
                },
                "no_write_evidence": {"proof_of_absence": True},
            }
        return {
            "post_id": post_id,
            "classification": "COMMITTED_NO_REPLAY",
            "registry": {"present": True, "schema_complete": True},
            "wordpress": {"status": "pending"},
            "journal": {"present": True, "run_id": RUNS.get(post_id, "")},
        }

    monkeypatch.setattr(recovery, "_round_post_evidence", fake)
    kwargs = _kwargs(root)
    kwargs.update(
        committed_post_ids=[101, 102, 103, 104],
        uncertain_post_ids=[],
        not_executed_post_ids=[105],
        run_ids=RUNS,
    )
    preview = recovery.recover_blocked_cycle(**kwargs)
    assert preview["status"] == "preview"
    assert preview["round_classifications"]["NOT_EXECUTED"] == [105]


@pytest.mark.parametrize("target", ["transition", "archive-manifest.json", "v2-quarantine.json", "state.json", "journal.jsonl"])
def test_resume_after_each_durable_write_boundary(tmp_path, monkeypatch, target):
    root = _root(tmp_path); _evidence(monkeypatch)
    preview = recovery.recover_blocked_cycle(**_kwargs(root))
    original = recovery.atomic_write_text
    fired = False
    def fail_once(path, text):
        nonlocal fired
        path = Path(path)
        matches = ((target == "transition" and path.parent.name == "transitions") or path.name == target)
        if matches and not fired:
            fired = True
            monkeypatch.setattr(recovery, "atomic_write_text", original)
            raise OSError(f"simulated interruption at {target}")
        return original(path, text)
    monkeypatch.setattr(recovery, "atomic_write_text", fail_once)
    with pytest.raises(OSError):
        recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))
    resumed = recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))
    assert resumed["status"] == "applied"


def test_resume_refuses_concurrent_quarantine_change(tmp_path, monkeypatch):
    root = _root(tmp_path); _evidence(monkeypatch)
    preview = recovery.recover_blocked_cycle(**_kwargs(root))
    original = recovery.atomic_write_text
    def fail_quarantine(path, text):
        if Path(path) == root / "config" / "v2-quarantine.json":
            monkeypatch.setattr(recovery, "atomic_write_text", original)
            raise OSError("stop before quarantine")
        return original(path, text)
    monkeypatch.setattr(recovery, "atomic_write_text", fail_quarantine)
    with pytest.raises(OSError):
        recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))
    policy_path = root / "config" / "v2-quarantine.json"
    policy = json.loads(policy_path.read_text()); policy["posts"].append({"post_id": 999, "reason_code": "concurrent", "reason": "unexpected"})
    policy_path.write_text(json.dumps(policy))
    with pytest.raises(recovery.CycleRecoveryError, match="diverged"):
        recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))


def test_completed_transition_is_auditable_after_new_cycle_advance_and_tamper_fails(tmp_path, monkeypatch):
    root = _root(tmp_path); _evidence(monkeypatch)
    preview = recovery.recover_blocked_cycle(**_kwargs(root))
    applied = recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))
    (root / "work" / "v2-cycle" / "state.json").write_text(json.dumps({"cycle_id": "later-cycle", "status": "running"}))
    (root / "work" / "v2-cycle" / "journal.jsonl").write_text("later\n")
    assert recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))["status"] == "already_applied"
    archive = Path(applied["archive_dir"])
    (archive / "new-state.json").write_text("tampered")
    with pytest.raises(recovery.CycleRecoveryError, match="hash"):
        recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))


def test_declared_run_id_must_match_journal(tmp_path, monkeypatch):
    root = _root(tmp_path); _evidence(monkeypatch)
    with pytest.raises(recovery.CycleRecoveryError, match="run_id"):
        recovery.recover_blocked_cycle(**_kwargs(root, run_ids={**RUNS, 101: "wrong"}))


def test_real_reconciler_fixture_uses_journals_registry_and_wordpress_readback(tmp_path):
    root = _root(tmp_path)
    # Replace the deliberately small DB with the schema consumed by transaction_admin.
    db = root / "work" / "media_registry.sqlite"
    db.unlink()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE media_runs (run_id TEXT, post_id INTEGER, created_at TEXT, cycle_id TEXT, cycle_round INTEGER, authorization_id TEXT, acquisition_outcome TEXT)")
        conn.execute("CREATE TABLE media_decisions (run_id TEXT, post_id INTEGER, created_at TEXT, candidate_id TEXT, disposition TEXT)")
        conn.execute("CREATE TABLE media_usage (post_id INTEGER, status TEXT, applied_at TEXT, run_id TEXT, cycle_id TEXT, cycle_round INTEGER, authorization_id TEXT, accepted_at TEXT)")
        conn.execute("CREATE TABLE media_operations (post_id INTEGER, run_id TEXT, cycle_id TEXT, cycle_round INTEGER, authorization_id TEXT, started_at TEXT, operation_status TEXT, write_started INTEGER, readback_confirmed INTEGER, completed_at TEXT, outcome TEXT)")
        for post_id in (101, 102, 103, 104):
            run_id = RUNS[post_id]
            conn.execute("INSERT INTO media_runs VALUES (?,?,?,?,?,?,?)", (run_id, post_id, "2026-01-01T00:00:00Z", CYCLE, 3, AUTH, "started"))
            conn.execute("INSERT INTO media_operations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (post_id, run_id, CYCLE, 3, AUTH, "2026-01-01T00:00:00Z", "started", 0, 0, None, "started"))
        conn.commit()
    (root / "work" / "v2-cycle" / "journal.jsonl").write_text(json.dumps({"event": "round_authorized", "cycle_id": CYCLE, "round_number": 3, "authorization_id": AUTH, "at": "2026-01-01T00:00:00+00:00"}) + "\n")
    state_value = {"phase": "media", "state": "pending"}
    for post_id in (101, 102, 103, 104):
        (root / "work" / "v2-journal" / f"{post_id}.json").write_text(json.dumps({
            "post_id": post_id, "run_id": RUNS[post_id], "cycle_id": CYCLE, "cycle_round": 3,
            "authorization_id": AUTH, "status": "committed" if post_id != 104 else "running",
            "writer_started": True if post_id != 104 else False, "readback": True if post_id != 104 else None,
            "state": state_value,
        }))
    class FixtureClient:
        def get_post(self, post_id):
            return {"id": post_id, "status": "pending", "meta": {"_hermes_work_state": state_value}, "content": {"raw": "body"}, "title": {"raw": "title"}, "featured_media": 0}
    result = recovery.recover_blocked_cycle(
        root, FixtureClient(), cycle_id=CYCLE, authorization_id=AUTH, pending_round=3,
        selected_ids=SELECTED, committed_post_ids=[101, 102, 103], uncertain_post_ids=[104],
        not_executed_post_ids=[105], pre_writer_recoverable_post_ids=[], run_ids=RUNS,
        operator_authorization="fixture", new_cycle_id="n" * 32,
    )
    assert result["classification"] == {"101": "COMMITTED_NO_REPLAY", "102": "COMMITTED_NO_REPLAY", "103": "COMMITTED_NO_REPLAY", "104": "UNCERTAIN_ISOLATED", "105": "NOT_EXECUTED"}


def test_automatic_recovery_classifies_full_round_and_isolates_only_uncertain_post(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _evidence(monkeypatch)
    session_lock = RunSessionLock(root / "work" / "v2-run.lock")
    assert session_lock.acquire()
    handles, locked_post_id, _error = _reserve_post_lock_batch(root, SELECTED, lock_ttl=60)
    assert locked_post_id is None
    try:
        result = recovery.auto_recover_blocked_cycle(
            root,
            object(),
            cycle_id=CYCLE,
            authorization_id=AUTH,
            pending_round=3,
            selected_ids=SELECTED,
            session_lock=session_lock,
            post_lock_handles=handles,
        )
    finally:
        _release_post_lock_batch(handles)
        session_lock.release()

    assert result["status"] == "applied"
    assert result["wordpress_writes"] == 0
    assert result["replay"] is False
    assert result["isolated_post_ids"] == [104]
    assert result["round_classifications"]["COMMITTED_NO_REPLAY"] == [101, 102, 103]
    state = json.loads((root / "work" / "v2-cycle" / "state.json").read_text())
    assert state["cycle_id"] == result["new_cycle_id"]
    assert state["status"] == "running"
    assert state["phase"] == "ADMISSION"
    journal_lines = (root / "work" / "v2-cycle" / "journal.jsonl").read_text().splitlines()
    assert len(journal_lines) == 1
    journal = json.loads(journal_lines[0])
    assert journal["recovery_mode"] == "automatic"
    policy = json.loads((root / "config" / "v2-quarantine.json").read_text())
    assert [row["post_id"] for row in policy["posts"]][-1] == 104


def test_automatic_recovery_refuses_incomplete_registry_without_mutating_cycle(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _evidence(monkeypatch)
    original_state = (root / "work" / "v2-cycle" / "state.json").read_bytes()
    original_policy = (root / "config" / "v2-quarantine.json").read_bytes()

    def incomplete(*args, post_id, **kwargs):
        row = {
            "post_id": post_id,
            "classification": "UNCERTAIN_ISOLATED",
            "registry": {"present": True, "schema_complete": False},
            "journal": {"present": True, "run_id": RUNS.get(post_id, "")},
        }
        return row

    monkeypatch.setattr(recovery, "_round_post_evidence", incomplete)
    session_lock = RunSessionLock(root / "work" / "v2-run.lock")
    assert session_lock.acquire()
    handles, locked_post_id, _error = _reserve_post_lock_batch(root, SELECTED, lock_ttl=60)
    assert locked_post_id is None
    try:
        result = recovery.auto_recover_blocked_cycle(
            root,
            object(),
            cycle_id=CYCLE,
            authorization_id=AUTH,
            pending_round=3,
            selected_ids=SELECTED,
            session_lock=session_lock,
            post_lock_handles=handles,
        )
    finally:
        _release_post_lock_batch(handles)
        session_lock.release()

    assert result["status"] == "blocked"
    assert result["reason"] == "registry_evidence_unavailable"
    assert (root / "work" / "v2-cycle" / "state.json").read_bytes() == original_state
    assert (root / "config" / "v2-quarantine.json").read_bytes() == original_policy
    assert not list((root / "work" / "v2-cycle" / "transitions").glob("*.json"))


def test_next_cycle_invocation_resumes_prepared_transition_before_reading_state(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _evidence(monkeypatch)
    preview = recovery.recover_blocked_cycle(**_kwargs(root))
    original = recovery.atomic_write_text

    def interrupt_before_checkpoint(path, text):
        if Path(path) == root / "work" / "v2-cycle" / "state.json":
            monkeypatch.setattr(recovery, "atomic_write_text", original)
            raise OSError("simulated interruption after quarantine")
        return original(path, text)

    monkeypatch.setattr(recovery, "atomic_write_text", interrupt_before_checkpoint)
    with pytest.raises(OSError, match="simulated interruption"):
        recovery.recover_blocked_cycle(**_kwargs(root, preview=preview, apply=True))

    session_lock = RunSessionLock(root / "work" / "v2-run.lock")
    assert session_lock.acquire()
    try:
        resumed = recovery.resume_staged_cycle_recovery(root, session_lock=session_lock)
    finally:
        session_lock.release()

    assert resumed["status"] == "resumed"
    assert resumed["wordpress_writes"] == 0
    assert json.loads((root / "work" / "v2-cycle" / "state.json").read_text())["cycle_id"] == "n" * 32


def test_next_cycle_revalidates_automatic_intent_before_reading_old_checkpoint(
    tmp_path, monkeypatch
):
    root = _root(tmp_path)
    _evidence(monkeypatch)
    original_copy = recovery._copy_verified
    interrupted = False

    def fail_after_intent(source, destination, hashes):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError("simulated interruption after automatic intent")
        return original_copy(source, destination, hashes)

    monkeypatch.setattr(recovery, "_copy_verified", fail_after_intent)
    session_lock = RunSessionLock(root / "work" / "v2-run.lock")
    assert session_lock.acquire()
    handles, locked_post_id, _error = _reserve_post_lock_batch(
        root, SELECTED, lock_ttl=60
    )
    assert locked_post_id is None
    try:
        with pytest.raises(OSError, match="after automatic intent"):
            recovery.auto_recover_blocked_cycle(
                root,
                object(),
                cycle_id=CYCLE,
                authorization_id=AUTH,
                pending_round=3,
                selected_ids=SELECTED,
                session_lock=session_lock,
                post_lock_handles=handles,
            )
    finally:
        _release_post_lock_batch(handles)
        session_lock.release()

    transition_path = next((root / "work" / "v2-cycle" / "transitions").glob("*.json"))
    assert json.loads(transition_path.read_text())["phase"] == "intent"

    resumed_lock = RunSessionLock(root / "work" / "v2-run.lock")
    assert resumed_lock.acquire()
    try:
        resumed = recovery.resume_staged_cycle_recovery(
            root, session_lock=resumed_lock, client=object()
        )
    finally:
        resumed_lock.release()

    assert resumed["status"] == "resumed"
    assert resumed["wordpress_writes"] == 0
    assert resumed["replay"] is False
    state = json.loads((root / "work" / "v2-cycle" / "state.json").read_text())
    assert state["cycle_id"] == resumed["new_cycle_id"]
    assert json.loads(transition_path.read_text())["phase"] == "completed"


def test_next_cycle_blocks_incomplete_operator_intent_without_replay(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _evidence(monkeypatch)
    original_copy = recovery._copy_verified

    def fail_after_intent(_source, _destination, _hashes):
        raise OSError("simulated interruption after operator intent")

    monkeypatch.setattr(recovery, "_copy_verified", fail_after_intent)
    with pytest.raises(OSError, match="after operator intent"):
        recovery.recover_blocked_cycle(
            **_kwargs(root, apply=True, preview=recovery.recover_blocked_cycle(**_kwargs(root)))
        )
    monkeypatch.setattr(recovery, "_copy_verified", original_copy)
    old_state = (root / "work" / "v2-cycle" / "state.json").read_bytes()

    session_lock = RunSessionLock(root / "work" / "v2-run.lock")
    assert session_lock.acquire()
    try:
        result = recovery.resume_staged_cycle_recovery(
            root, session_lock=session_lock, client=object()
        )
    finally:
        session_lock.release()

    assert result["status"] == "blocked"
    assert result["reason"] == "incomplete_operator_transition_requires_explicit_recovery"
    assert result["wordpress_writes"] == 0
    assert (root / "work" / "v2-cycle" / "state.json").read_bytes() == old_state
