import hashlib
import json
from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2.cycle_recovery import (
    CycleRecoveryError,
    recover_blocked_cycle,
)
from unicornio_editor.pipeline_v2.cycle_state import CycleState, CycleStateStore


CYCLE = "old-cycle"
AUTH = "old-auth"
SELECTED = [101, 102, 103]
RUNS = {101: "run-101", 102: "run-102"}


def make_root(tmp_path: Path) -> Path:
    root = tmp_path
    (root / "config").mkdir(exist_ok=True)
    (root / "work" / "v2-cycle").mkdir(parents=True)
    (root / "work" / "v2-journal").mkdir(parents=True)
    (root / "work" / "v2-editorial" / "101").mkdir(parents=True)
    (root / "work" / "batches" / "v2-101-inline").mkdir(parents=True)
    (root / "work" / "v2-run.lock").write_text("", encoding="utf-8")
    (root / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}) + "\n",
        encoding="utf-8",
    )
    state = CycleState(
        cycle_id=CYCLE,
        phase="ADMISSION",
        pending_round={
            "round_number": 3,
            "authorization_id": AUTH,
            "preview_selected_ids": SELECTED,
        },
    )
    store = CycleStateStore(root)
    store.save(state, event="round_authorized", round_number=3, selected_ids=SELECTED, authorization_id=AUTH)
    (root / "work" / "v2-journal" / "101.json").write_text(json.dumps({
        "post_id": 101, "run_id": RUNS[101], "cycle_id": CYCLE,
        "cycle_round": 3, "authorization_id": AUTH, "status": "committed",
        "writer_started": True, "readback": True,
    }) + "\n", encoding="utf-8")
    (root / "work" / "v2-journal" / "102.json").write_text(json.dumps({
        "post_id": 102, "run_id": RUNS[102], "cycle_id": CYCLE,
        "cycle_round": 3, "authorization_id": AUTH, "status": "running",
        "writer_started": False, "readback": None,
    }) + "\n", encoding="utf-8")
    (root / "work" / "v2-editorial" / "101" / "partial.json").write_text("evidence", encoding="utf-8")
    (root / "work" / "batches" / "v2-101-inline" / "manifest.json").write_text("media", encoding="utf-8")
    return root


def call(root: Path, **kwargs):
    return recover_blocked_cycle(
        root,
        cycle_id=CYCLE,
        authorization_id=AUTH,
        pending_round=3,
        selected_ids=SELECTED,
        uncertain_post_ids=[101, 102],
        not_executed_post_ids=[103],
        run_ids=RUNS,
        operator_authorization="ticket-recovery-1",
        new_cycle_id="new-cycle",
        **kwargs,
    )


def test_preview_is_read_only(tmp_path):
    root = make_root(tmp_path)
    before = {p: p.read_bytes() for p in [root / "work/v2-cycle/state.json", root / "work/v2-cycle/journal.jsonl", root / "config/v2-quarantine.json"]}
    result = call(root)
    assert result["status"] == "preview"
    assert result["wordpress_writes"] == 0
    assert {p: p.read_bytes() for p in before} == before


def test_apply_archives_hashes_quarantines_uncertain_and_starts_cycle(tmp_path):
    root = make_root(tmp_path)
    old_state = (root / "work/v2-cycle/state.json").read_bytes()
    old_journal = (root / "work/v2-cycle/journal.jsonl").read_bytes()
    result = call(root, apply=True)
    assert result["status"] == "applied"
    assert result["wordpress_writes"] == 0
    assert result["replay"] is False
    archive = Path(result["archive_dir"])
    manifest = json.loads((archive / "archive-manifest.json").read_text())
    assert manifest["old_state_sha256"] == hashlib.sha256(old_state).hexdigest()
    assert manifest["old_journal_sha256"] == hashlib.sha256(old_journal).hexdigest()
    assert (archive / "evidence" / "work" / "v2-journal" / "101.json").is_file()
    policy = json.loads((root / "config/v2-quarantine.json").read_text())
    assert [row["post_id"] for row in policy["posts"]] == [101, 102]
    state = json.loads((root / "work/v2-cycle/state.json").read_text())
    assert state["cycle_id"] == "new-cycle"
    assert state["pending_round"] is None
    assert state["ready_post_ids"] == []
    assert json.loads((root / "work/v2-cycle/v2-cycle-recovery-transition.json").read_text())["status"] == "completed"


def test_requires_complete_reconciliation_partition(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(CycleRecoveryError, match="exactly one"):
        recover_blocked_cycle(
            root, cycle_id=CYCLE, authorization_id=AUTH, pending_round=3,
            selected_ids=SELECTED, uncertain_post_ids=[101], not_executed_post_ids=[103],
            run_ids=RUNS, operator_authorization="ticket", new_cycle_id="new",
        )


def test_rejects_changed_uncertain_journal(tmp_path):
    root = make_root(tmp_path)
    data = json.loads((root / "work/v2-journal/101.json").read_text())
    data["run_id"] = "other-run"
    (root / "work/v2-journal/101.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CycleRecoveryError, match="identity mismatch"):
        call(root)
