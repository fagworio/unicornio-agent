import json

import pytest

from unicornio_editor.pipeline_v2.cycle import CycleConfig, CycleRoundResult, run_cycle
from unicornio_editor.pipeline_v2.cycle_state import CycleState, CycleStateStore


def _epoch(tmp_path):
    path = tmp_path / "work" / "v2-epochs" / "active" / "epoch.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "epoch_id": "test-epoch", "protected_transaction_ids": [115757]}), encoding="utf-8")


def retry_detail(post_id, *, progress=True):
    return {
        "post_id": post_id,
        "initial_state": "pending",
        "outcome": "retry",
        "progress": progress,
    }


def test_cycle_state_store_writes_atomic_checkpoint_and_append_only_journal(tmp_path):
    _epoch(tmp_path)
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="cycle-test")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    store.authorize_round(state, [11])

    restored = store.load()
    assert restored is not None
    assert restored.cycle_id == "cycle-test"
    assert restored.pending_round["preview_selected_ids"] == [11]
    assert restored.pending_round["authorization_id"]

    store.complete_round(state, report={"round": 1, "executed_ids": [11]})
    store.finish(state, status="completed", stop_reason="no_eligible_work")
    restored = store.load()
    assert restored.status == "completed"
    assert restored.pending_round is None
    events = [json.loads(line)["event"] for line in store.journal_path.read_text().splitlines()]
    assert events == [
        "cycle_started",
        "round_started",
        "round_authorized",
        "round_completed",
        "cycle_finished",
    ]


def test_cycle_resumes_accounting_without_replaying_completed_rounds():
    state = CycleState(
        cycle_id="resume-me",
        rounds=1,
        progress_rounds=1,
        seen_post_ids=[1],
        ready_post_ids=[],
        attempts_by_post={1: 1},
        round_reports=[{"round": 1, "executed_ids": [1]}],
    )
    requests = []

    def execute(request):
        requests.append(request)
        return CycleRoundResult(
            preview_selected_ids=(2,),
            executed_ids=(2,),
            details=(retry_detail(2),),
        )

    report = run_cycle(
        execute,
        state=state,
        config=CycleConfig(ready_target=2, max_total_attempts=2),
    )

    assert report.resumed is True
    assert report.rounds == 2
    assert report.attempts_by_post == {1: 1, 2: 1}
    assert requests[0].round_number == 2
    assert requests[0].seen_post_ids == (1,)


def test_cycle_state_rejects_duplicate_ids_and_unknown_schema(tmp_path):
    with pytest.raises(ValueError):
        CycleState(cycle_id="x", seen_post_ids=[1, 1])
    store = CycleStateStore(tmp_path)
    store.state_path.parent.mkdir(parents=True)
    store.state_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        store.load()


def test_cycle_accumulates_persisted_duration_on_resume():
    state = CycleState(
        cycle_id="timed",
        elapsed_seconds=9.0,
        rounds=0,
    )
    now = [0.0]

    def clock():
        return now[0]

    def execute(_request):
        now[0] = 2.0
        return CycleRoundResult(
            preview_selected_ids=(1,),
            executed_ids=(1,),
            details=(retry_detail(1),),
        )

    report = run_cycle(
        execute,
        state=state,
        clock=clock,
        config=CycleConfig(ready_target=2, max_duration_seconds=10.0),
    )
    assert report.stop_reason == "duration_limit"
    assert report.elapsed_seconds == 11.0


def test_cycle_store_does_not_charge_pause_between_cron_invocations(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="paused")
    state.elapsed_seconds = 9.0
    state.active_started_at = "2000-01-01T00:00:00+00:00"
    store.begin_run(state)
    assert state.elapsed_seconds == 9.0
    assert state.active_started_at is not None


def test_cycle_store_invalidates_authorization_as_distinct_audit_event(tmp_path):
    _epoch(tmp_path)
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="invalidate")
    store.start_round(state, round_number=4, request={"max_new_posts": 0})
    store.authorize_round(state, [101, 102])
    store.invalidate_round_authorization(
        state,
        reason="unique_post_budget_violation",
        selected_ids=[101, 102],
        evidence={"journal_files_absent": True},
    )
    restored = store.load()
    assert restored.pending_round is None
    events = [json.loads(line) for line in store.journal_path.read_text().splitlines()]
    assert events[-1]["event"] == "round_authorization_invalidated"
    assert events[-1]["executed_ids"] == []
    assert not any(event["event"] == "round_completed" for event in events)
