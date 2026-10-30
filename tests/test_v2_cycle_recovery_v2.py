from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2 import cycle_recovery_v2 as recovery

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
        row = {"post_id": post_id, "classification": classes[post_id], "registry": {"present": True}, "wordpress": {"status": "pending"}}
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
    })
    with pytest.raises(recovery.CycleRecoveryError, match="MediaRegistry"):
        recovery.recover_blocked_cycle(**_kwargs(root, committed_post_ids=[101, 102, 103, 104], uncertain_post_ids=[], not_executed_post_ids=[105]))
