import json
from types import SimpleNamespace

from unicornio_editor.pipeline_v2 import cycle_runtime
from unicornio_editor.pipeline_v2.lock import RunSessionLock
from unicornio_editor.pipeline_v2.model import LifecycleState, Phase, WorkState


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
    assert all(call["cycle_id"] == report.cycle_id for call in calls)
    assert [call["cycle_round"] for call in calls] == [1, 1, 2, 2]
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


def test_reconcile_completed_round_rebuilds_mixed_post_results_from_journals(tmp_path):
    class Client:
        def __init__(self):
            self.posts = {
                1: {
                    "id": 1, "status": "pending", "title": {"raw": "A"},
                    "content": {"raw": "<p>a</p>"},
                    "meta": {"_hermes_work_state": json.dumps(
                        WorkState(state=LifecycleState.READY, phase=Phase.VALIDATE,
                                  relevance_approved=True).to_dict()
                    )},
                },
                2: {
                    "id": 2, "status": "pending", "title": {"raw": "B"},
                    "content": {"raw": "<p>b</p>"},
                    "meta": {"_hermes_work_state": json.dumps(
                        WorkState(state=LifecycleState.PENDING, phase=Phase.MEDIA,
                                  relevance_approved=True).to_dict()
                    )},
                },
            }

        def list_pending(self, *, page, per_page, status):
            return list(self.posts.values()) if page == 1 else []

        def get_post(self, post_id):
            return self.posts[post_id]

    client = Client()
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    for post_id, state in (
        (1, LifecycleState.READY),
        (2, LifecycleState.PENDING),
    ):
        journal_state = WorkState(
            state=state,
            phase=Phase.VALIDATE if state is LifecycleState.READY else Phase.MEDIA,
            relevance_approved=True,
        )
        (journal_dir / f"{post_id}.json").write_text(json.dumps({
            "status": "committed",
            "readback": True,
            "state": journal_state.to_dict(),
            "media_readback_status": "confirmed" if post_id == 2 else "not_attempted",
        }), encoding="utf-8")

    result = cycle_runtime._reconcile_completed_round(client, tmp_path, [1, 2])
    assert result is not None
    assert result.executed_ids == (1, 2)
    assert [detail["outcome"] for detail in result.details] == ["ready", "retry"]
    assert result.details[0]["ready_verified"] is True
    assert result.details[1]["progress"] is True


def test_cycle_does_not_touch_checkpoint_when_lock_is_busy(tmp_path):
    lock = RunSessionLock(tmp_path / "work" / "v2-run.lock")
    assert lock.acquire() is True
    try:
        result = cycle_runtime.run_v2_cycle(object(), _config(), tmp_path)
    finally:
        lock.release()
    assert result["locked"] is True
    assert not (tmp_path / "work" / "v2-cycle" / "state.json").exists()
