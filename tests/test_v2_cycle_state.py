import json

import pytest

from unicornio_editor.pipeline_v2.cycle import CycleConfig, CycleRoundResult, run_cycle
from unicornio_editor.pipeline_v2.cycle_state import CycleState, CycleStateStore


def retry_detail(post_id, *, progress=True):
    return {
        "post_id": post_id,
        "initial_state": "pending",
        "outcome": "retry",
        "progress": progress,
    }


def test_cycle_state_store_writes_atomic_checkpoint_and_append_only_journal(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="cycle-test")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    store.authorize_round(state, [11])

    restored = store.load()
    assert restored is not None
    assert restored.cycle_id == "cycle-test"
    assert restored.pending_round["preview_selected_ids"] == [11]

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
