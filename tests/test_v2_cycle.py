from unicornio_editor.pipeline_v2.cycle import CycleConfig, CycleRoundResult, run_cycle


def ready_detail(post_id, *, initial_state="pending", progress=True):
    return {
        "post_id": post_id,
        "initial_state": initial_state,
        "outcome": "ready",
        "readback": True,
        "state": {"state": "ready", "phase": "validate"},
        "progress": progress,
    }


def retry_detail(post_id, *, progress=True):
    return {
        "post_id": post_id,
        "initial_state": "pending",
        "outcome": "retry",
        "progress": progress,
    }


def test_cycle_replaces_completed_posts_and_counts_ready_once():
    rounds = [
        CycleRoundResult(
            preview_selected_ids=(1, 2, 3, 4, 5),
            executed_ids=(1, 2, 3, 4, 5),
            details=tuple(retry_detail(post_id) for post_id in range(1, 6)),
        ),
        CycleRoundResult(
            preview_selected_ids=(1, 2, 3, 4, 6),
            executed_ids=(1, 2, 3, 4, 6),
            details=(
                ready_detail(1),
                retry_detail(2),
                retry_detail(3),
                retry_detail(4),
                retry_detail(6),
            ),
        ),
        CycleRoundResult(
            preview_selected_ids=(2, 3, 4, 6, 7),
            executed_ids=(2, 3, 4, 6, 7),
            details=(
                ready_detail(2),
                ready_detail(3),
                retry_detail(4),
                retry_detail(6),
                retry_detail(7),
            ),
        ),
    ]

    def execute(request):
        assert request.round_number == len(requests) + 1
        requests.append(request)
        return rounds.pop(0)

    requests = []
    report = run_cycle(
        execute,
        config=CycleConfig(
            ready_target=3,
            max_unique_posts=9,
            max_total_attempts=20,
            max_attempts_per_post_per_cycle=3,
        ),
    )

    assert report.stop_reason == "ready_target"
    assert report.ready_post_ids == [1, 2, 3]
    assert report.ready_count == 3
    assert report.unique_posts == 7
    assert report.attempts_total == 15
    assert requests[1].seen_post_ids == (1, 2, 3, 4, 5)


def test_cycle_excludes_posts_that_reached_attempt_budget():
    requests = []

    def execute(request):
        requests.append(request)
        if request.round_number == 1:
            return CycleRoundResult(
                preview_selected_ids=(10,),
                executed_ids=(10,),
                details=(retry_detail(10),),
            )
        if request.round_number == 2:
            assert 10 not in request.excluded_post_ids
            return CycleRoundResult(
                preview_selected_ids=(10,),
                executed_ids=(10,),
                details=(retry_detail(10),),
            )
        assert 10 in request.excluded_post_ids
        return CycleRoundResult()

    report = run_cycle(
        execute,
        config=CycleConfig(
            ready_target=2,
            new_batch_size=2,
            max_total_attempts=5,
            max_attempts_per_post_per_cycle=2,
        ),
    )

    assert report.stop_reason == "no_eligible_work"
    assert report.attempts_by_post == {10: 2}


def test_cycle_fails_closed_when_preview_and_write_ids_diverge():
    def execute(_request):
        return CycleRoundResult(
            preview_selected_ids=(1, 2),
            executed_ids=(1, 3),
            details=(retry_detail(1), retry_detail(3)),
        )

    report = run_cycle(execute)

    assert report.stop_reason == "selection_mismatch"
    assert report.phase == "ADMISSION"
    assert report.rounds == 0
    assert report.attempts_total == 0


def test_cycle_requires_a_readback_detail_for_each_executed_post():
    def execute(_request):
        return CycleRoundResult(
            preview_selected_ids=(1, 2),
            executed_ids=(1, 2),
            details=(retry_detail(1),),
        )

    report = run_cycle(execute)

    assert report.stop_reason == "incomplete_round"
    assert report.attempts_total == 0


def test_cycle_stops_after_stagnant_rounds_without_ready():
    requests = []

    def execute(request):
        requests.append(request)
        post_id = 1
        return CycleRoundResult(
            preview_selected_ids=(post_id,),
            executed_ids=(post_id,),
            details=(retry_detail(post_id, progress=False),),
        )

    report = run_cycle(
        execute,
        config=CycleConfig(
            ready_target=5,
            max_stagnant_rounds=2,
            max_drain_rounds=2,
            max_attempts_per_post_per_cycle=10,
            max_unique_posts=2,
            max_total_attempts=10,
        ),
    )

    assert report.stop_reason == "drain_limit"
    assert report.rounds == 4
    assert report.stagnant_rounds == 4
    assert report.drain_rounds == 2
    assert [request.max_new_posts for request in requests] == [2, 1, 0, 0]
    assert all(
        entry["phase"] == ("ADMISSION" if entry["round"] <= 2 else "DRAINING")
        for entry in report.round_reports
    )


def test_cycle_respects_total_attempt_budget():
    calls = []

    def execute(request):
        calls.append(request)
        ids = tuple(range(1, request.batch_size + 1))
        return CycleRoundResult(
            preview_selected_ids=ids,
            executed_ids=ids,
            details=tuple(retry_detail(post_id) for post_id in ids),
        )

    report = run_cycle(
        execute,
        config=CycleConfig(
            ready_target=5,
            new_batch_size=3,
            max_total_attempts=4,
            max_unique_posts=10,
        ),
    )

    assert report.stop_reason == "attempt_budget"
    assert report.attempts_total == 4
    assert calls[1].batch_size == 1
