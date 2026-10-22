import json
from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2.cycle_admin import CycleAbandonError, abandon_cycle
from unicornio_editor.pipeline_v2.cycle_state import CycleState, CycleStateStore


OLD = "old-cycle"
AUTH = "old-auth"
SELECTED = [115592, 115629, 115282, 115631, 115530]
NEW = "new-cycle"


def _fixture(root: Path):
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [{"post_id": 115152, "reason_code": "existing", "reason": "existing"}], "release_history": []}),
        encoding="utf-8",
    )
    store = CycleStateStore(root)
    state = CycleState(cycle_id=OLD, phase="DRAINING", status="running", config={"ready_target": 5})
    state.rounds = 3
    state.seen_post_ids = list(SELECTED)
    state.pending_round = {
        "round_number": 4,
        "authorization_id": AUTH,
        "preview_selected_ids": list(SELECTED),
        "request": {"max_new_posts": 0, "batch_size": 5},
    }
    store.save(state, event="round_authorized", round_number=4, authorization_id=AUTH, selected_ids=SELECTED)
    return store


def _args(root):
    return dict(
        root=root,
        cycle_id=OLD,
        authorization_id=AUTH,
        selected_ids=SELECTED,
        pending_round=4,
        new_cycle_id=NEW,
        exclude_post_id=115282,
    )


def test_cycle_abandon_preview_is_read_only(tmp_path):
    _fixture(tmp_path)
    before = (tmp_path / "work" / "v2-cycle" / "state.json").read_bytes()
    result = abandon_cycle(**_args(tmp_path), apply=False)
    assert result["status"] == "preview"
    assert result["global_lock"] == "available"
    assert result["wordpress_writes"] == 0
    assert (tmp_path / "work" / "v2-cycle" / "state.json").read_bytes() == before
    assert not (tmp_path / "work" / "v2-cycle" / "archives").exists()


def test_cycle_abandon_archives_once_starts_admission_and_excludes_only_target(tmp_path):
    _fixture(tmp_path)
    result = abandon_cycle(**_args(tmp_path), apply=True)
    assert result["status"] == "applied"
    assert result["new_cycle_id"] == NEW
    assert result["post_115282_excluded"] is True

    state = CycleStateStore(tmp_path).load()
    assert state is not None
    assert state.cycle_id == NEW
    assert state.phase == "ADMISSION"
    assert state.pending_round is None
    policy = json.loads((tmp_path / "config" / "v2-quarantine.json").read_text())
    assert {row["post_id"] for row in policy["posts"]} == {115152, 115282}
    archive = Path(result["archive_dir"])
    manifest = json.loads((archive / "archive-manifest.json").read_text())
    assert manifest["archival_status"] == "ABANDONED_BY_OPERATOR"
    assert manifest["round_4_not_falsified"] is True
    assert json.loads((archive / "state.json").read_text())["cycle_id"] == OLD

    again = abandon_cycle(**_args(tmp_path), apply=True)
    assert again["status"] == "already_applied"
    assert again["archive_dir"] == result["archive_dir"]


def test_cycle_abandon_recovers_prepared_transition_after_interruption(tmp_path, monkeypatch):
    _fixture(tmp_path)
    import unicornio_editor.pipeline_v2.cycle_admin as admin

    original = admin._apply_from_transition
    calls = {"count": 0}

    def interrupt_once(root, transition):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated crash")
        return original(root, transition)

    monkeypatch.setattr(admin, "_apply_from_transition", interrupt_once)
    with pytest.raises(RuntimeError, match="simulated crash"):
        abandon_cycle(**_args(tmp_path), apply=True)
    transition = json.loads((tmp_path / "work" / "v2-cycle" / admin.TRANSITION_NAME).read_text())
    assert transition["phase"] == "prepared"

    recovered = abandon_cycle(**_args(tmp_path), apply=True)
    assert recovered["status"] == "applied"
    assert CycleStateStore(tmp_path).load().cycle_id == NEW
