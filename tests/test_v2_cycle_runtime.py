import json
from types import SimpleNamespace

from unicornio_editor.pipeline_v2 import cycle_runtime


def _config():
    return SimpleNamespace(
        v2_cycle_ready_target=2,
        v2_cycle_new_batch_size=1,
        v2_cycle_max_unique_posts=2,
        v2_cycle_max_total_attempts=2,
        v2_cycle_max_attempts_per_post=1,
        v2_cycle_max_stagnant_rounds=2,
        v2_cycle_max_duration_seconds=60.0,
    )


def _ready_detail(post_id):
    return {
        "post_id": post_id,
        "initial_state": "pending",
        "outcome": "ready",
        "readback": True,
        "state": {"state": "ready", "phase": "validate"},
        "progress": True,
    }


def test_production_cycle_runs_pinned_preview_write_rounds_and_persists(tmp_path, monkeypatch):
    calls = []
    next_id = iter((101, 102))

    def fake_run_v2(client, config, root, **kwargs):
        calls.append(kwargs)
        post_id = (
            int(kwargs["expected_selected_ids"][0])
            if kwargs.get("expected_selected_ids")
            else next_id.__next__()
        )
        if kwargs.get("preview", False):
            return {"preview": True, "queue": {"selected_ids": [post_id]}}
        return {
            "details": [_ready_detail(post_id)],
            "locked": False,
            "selection_mismatch": False,
        }

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    report = cycle_runtime.run_v2_cycle(object(), _config(), tmp_path)

    assert report.stop_reason == "ready_target"
    assert report.ready_post_ids == [101, 102]
    assert len(calls) == 4
    assert all(call["lock_held"] is True for call in calls)
    assert [call["preview"] for call in calls] == [True, False, True, False]
    state = json.loads((tmp_path / "work" / "v2-cycle" / "state.json").read_text())
    assert state["status"] == "completed"
    assert state["stop_reason"] == "ready_target"
    assert state["rounds"] == 2


def test_production_cycle_refuses_fresh_when_interrupted_checkpoint_exists(tmp_path, monkeypatch):
    store = cycle_runtime.CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="interrupted")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    store.authorize_round(state, [10])

    monkeypatch.setattr(cycle_runtime, "run_v2", lambda *args, **kwargs: {})
    try:
        cycle_runtime.run_v2_cycle(object(), _config(), tmp_path, fresh=True)
    except ValueError as exc:
        assert "interrupted" in str(exc)
    else:
        raise AssertionError("fresh must not discard an interrupted cycle")
