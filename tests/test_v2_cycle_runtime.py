import json
import sqlite3
from types import SimpleNamespace

import pytest

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
    assert calls[0]["authorization_id"]
    assert calls[0]["authorization_id"] == calls[1]["authorization_id"]
    assert calls[2]["authorization_id"]
    assert calls[2]["authorization_id"] == calls[3]["authorization_id"]
    assert calls[0]["authorization_id"] != calls[2]["authorization_id"]
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
            "cycle_id": "cycle-test",
            "cycle_round": 1,
            "state": journal_state.to_dict(),
            "media_readback_status": "confirmed" if post_id == 2 else "not_attempted",
        }), encoding="utf-8")

    result = cycle_runtime._reconcile_completed_round(
        client,
        tmp_path,
        [1, 2],
        expected_cycle_id="cycle-test",
        expected_cycle_round=1,
    )
    assert result is not None
    assert result.executed_ids == (1, 2)
    assert [detail["outcome"] for detail in result.details] == ["ready", "retry"]
    assert result.details[0]["ready_verified"] is True
    assert result.details[1]["progress"] is True


def test_reconcile_completed_round_separates_journal_from_another_cycle(tmp_path):
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "1.json").write_text(json.dumps({
        "status": "committed",
        "readback": True,
        "cycle_id": "old-cycle",
        "cycle_round": 1,
    }), encoding="utf-8")

    class Client:
        def list_pending(self, *, page, per_page, status):
            return []

        def get_post(self, post_id):
            return {"id": post_id, "status": "pending"}

    result = cycle_runtime._reconcile_completed_round(
        Client(),
        tmp_path,
        [1],
        expected_cycle_id="new-cycle",
        expected_cycle_round=2,
    )
    assert result is not None
    assert result.reconciled_without_replay is True
    assert result.executed_ids == ()
    assert result.details[0]["outcome"] == "not_executed"


def test_reconcile_completed_round_rejects_matching_authorization_with_wrong_round(
    tmp_path,
):
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "1.json").write_text(json.dumps({
        "status": "committed",
        "readback": True,
        "cycle_id": "other-cycle",
        "cycle_round": 9,
        "authorization_id": "authorization-1",
    }), encoding="utf-8")

    class Client:
        def list_pending(self, *, page, per_page, status):
            return []

        def get_post(self, post_id):
            return {"id": post_id, "status": "pending"}

    result = cycle_runtime._reconcile_completed_round(
        Client(),
        tmp_path,
        [1],
        expected_cycle_id="current-cycle",
        expected_cycle_round=4,
        expected_authorization_id="authorization-1",
    )
    assert result is not None
    assert result.reconciliation_blocked is True
    assert result.reconciliation_blocking_info["unresolved_posts"] == [
        {"post_id": 1, "reason": "journal_uncorrelated"}
    ]


def test_cycle_does_not_touch_checkpoint_when_lock_is_busy(tmp_path):
    lock = RunSessionLock(tmp_path / "work" / "v2-run.lock")
    assert lock.acquire() is True
    try:
        result = cycle_runtime.run_v2_cycle(object(), _config(), tmp_path)
    finally:
        lock.release()
    assert result["locked"] is True
    assert not (tmp_path / "work" / "v2-cycle" / "state.json").exists()


def test_cycle_blocks_write_when_incomplete_journal_is_detected(tmp_path, monkeypatch):
    store = cycle_runtime.CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="journal-guard")
    state.phase = "DRAINING"
    state.seen_post_ids = [7]
    state.stagnant_rounds = 3
    store.save(state, event="seed_draining")
    calls = []

    def fake_run_v2(_client, _config, _root, **kwargs):
        calls.append(kwargs)
        if kwargs.get("preview"):
            return {"preview": True, "queue": {"selected_ids": [7]}}
        return {
            "preview": False,
            "journal_conflict": True,
            "journal_conflicts": [{
                "post_id": 7,
                "reason": "incomplete_journal",
                "status": "running",
            }],
            "details": [],
        }

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    report = cycle_runtime.run_v2_cycle(object(), _config(), tmp_path)

    assert report.stop_reason == "recovery_uncertain"
    assert [call["preview"] for call in calls] == [True, False]
    assert report.round_reports[0]["event"] == "round_reconciliation_blocked"
    assert report.round_reports[0]["journal_conflicts"] == [{
        "post_id": 7,
        "reason": "incomplete_journal",
        "status": "running",
    }]
    persisted = cycle_runtime.CycleStateStore(tmp_path).load()
    assert persisted.pending_round["preview_selected_ids"] == [7]


def test_draining_budget_is_checked_before_v2_write(tmp_path, monkeypatch):
    store = cycle_runtime.CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="draining-budget")
    state.phase = "DRAINING"
    state.seen_post_ids = [99]
    state.stagnant_rounds = 3
    store.save(state, event="test_draining")
    calls = []

    def fake_run_v2(_client, _config, _root, **kwargs):
        calls.append(kwargs)
        if kwargs.get("preview"):
            return {"preview": True, "queue": {"selected_ids": [101]}}
        raise AssertionError("write mode must not run after an admission violation")

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    report = cycle_runtime.run_v2_cycle(object(), _config(), tmp_path)

    assert report.stop_reason == "unique_post_budget_violation"
    assert len(calls) == 1
    assert calls[0]["preview"] is True
    persisted = cycle_runtime.CycleStateStore(tmp_path).load()
    assert persisted.status == "running"
    assert persisted.active_started_at is None
    assert persisted.pending_round["preview_selected_ids"] is None


def test_draining_executes_round_already_authorized_before_phase_change(tmp_path, monkeypatch):
    store = cycle_runtime.CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="draining-authorized")
    state.phase = "DRAINING"
    state.seen_post_ids = [99]
    state.stagnant_rounds = 3
    store.start_round(
        state,
        round_number=1,
        request={
            "round_number": 1,
            "batch_size": 1,
            "max_new_posts": 1,
            "seen_post_ids": [99],
            "excluded_post_ids": [],
            "attempts_by_post": {},
        },
    )
    store.authorize_round(state, [101])
    calls = []

    def fake_run_v2(_client, _config, _root, **kwargs):
        calls.append(kwargs)
        if kwargs.get("preview"):
            return {"preview": True, "queue": {"selected_ids": [101]}}
        return {
            "details": [_ready_detail(101)],
            "locked": False,
            "selection_mismatch": False,
        }

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    report = cycle_runtime.run_v2_cycle(object(), _config(), tmp_path)

    assert report.stop_reason == "ready_target"
    assert [call["preview"] for call in calls] == [True, False]
    assert [call["max_new_posts"] for call in calls] == [1, 1]
    assert [call["retry_post_ids"] for call in calls] == [(99,), (99,)]


def test_draining_invalid_authorization_is_audited_and_removed_without_write(tmp_path, monkeypatch):
    store = cycle_runtime.CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="invalidate-known-round")
    state.phase = "DRAINING"
    state.rounds = 3
    state.seen_post_ids = [1, 2, 3]
    state.attempts_by_post = {1: 1, 2: 1, 3: 1}
    state.stagnant_rounds = 3
    store.save(state, event="seed_draining")
    store.start_round(
        state,
        round_number=4,
        request={
            "round_number": 4,
            "batch_size": 2,
            "max_new_posts": 0,
            "seen_post_ids": [1, 2, 3],
            "excluded_post_ids": [],
            "attempts_by_post": {1: 1, 2: 1, 3: 1},
        },
    )
    store.authorize_round(state, [115329, 115288])
    calls = []

    class Client:
        def list_pending(self, *, page, per_page, status):
            return []

        def get_post(self, post_id):
            return {"id": post_id, "status": "pending"}

    def fake_run_v2(_client, _config, _root, **kwargs):
        calls.append(kwargs)
        if kwargs.get("expected_selected_ids"):
            return {
                "preview": True,
                "queue": {"selected_ids": []},
                "selection_mismatch": True,
            }
        return {"preview": True, "queue": {"selected_ids": []}}

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    report = cycle_runtime.run_v2_cycle(Client(), _config(), tmp_path)

    assert report.stop_reason == "no_eligible_work"
    assert report.rounds == 3
    assert len(calls) == 2
    assert all(call["preview"] is True for call in calls)
    persisted = cycle_runtime.CycleStateStore(tmp_path).load()
    assert persisted.pending_round is None
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "work" / "v2-cycle" / "journal.jsonl").read_text().splitlines()
    ]
    assert "round_authorization_invalidated" in events
    assert "round_completed" not in events


def test_selection_mismatch_invalidates_unexecuted_authorization_with_per_post_reasons(
    tmp_path, monkeypatch
):
    store = cycle_runtime.CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="stale-selection")
    state.phase = "DRAINING"
    state.rounds = 3
    state.seen_post_ids = [1, 2, 3, 115592, 115629, 115282, 115631, 115530]
    state.attempts_by_post = {1: 1, 2: 1, 3: 1}
    state.stagnant_rounds = 3
    store.save(state, event="seed_draining")
    store.start_round(
        state,
        round_number=4,
        request={
            "round_number": 4,
            "batch_size": 5,
            "max_new_posts": 0,
            "seen_post_ids": [1, 2, 3, 115592, 115629, 115282, 115631, 115530],
            "excluded_post_ids": [],
            "attempts_by_post": {1: 1, 2: 1, 3: 1},
        },
    )
    store.authorize_round(state, [115592, 115629, 115282, 115631, 115530])

    class Client:
        def list_pending(self, *, page, per_page, status):
            return []

        def get_post(self, post_id):
            return {"id": post_id, "status": "pending"}

    def fake_run_v2(_client, _config, _root, **kwargs):
        if kwargs.get("expected_selected_ids"):
            return {
                "preview": True,
                "selection_mismatch": True,
                "actual_selected_ids": [115282],
                "queue": {
                    "selected_ids": [115282],
                    "actual_selected_ids": [115282],
                    "eligible_ids": [115282, 115631],
                    "cooldown_ids": [115592],
                    "quarantined_ids": [115629],
                    "cycle_excluded_ids": [115530],
                },
            }
        return {"preview": True, "queue": {"selected_ids": []}}

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    report = cycle_runtime.run_v2_cycle(Client(), _config(), tmp_path)

    assert report.stop_reason == "no_eligible_work"
    assert report.rounds == 3
    assert report.attempts_total == 3
    assert report.round_reports[0]["event"] == "round_authorization_invalidated"
    evidence = report.round_reports[0]["evidence"]
    assert evidence["selection"]["reason_by_post"] == {
        "115592": "cooldown_active",
        "115629": "quarantined",
        "115282": "selection_order_changed",
        "115631": "selection_order_changed",
        "115530": "attempt_budget_excluded",
    }
    assert report.round_reports[0]["executed_ids"] == []
    persisted = cycle_runtime.CycleStateStore(tmp_path).load()
    assert persisted.pending_round is None
    events = [
        json.loads(line) for line in (tmp_path / "work" / "v2-cycle" / "journal.jsonl").read_text().splitlines()
    ]
    assert any(event["event"] == "round_authorization_invalidated" for event in events)
    assert not any(event["event"] == "round_completed" and event.get("round_number") == 4 for event in events)


def test_no_write_confirmation_ignores_journal_from_different_authorization(tmp_path):
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "101.json").write_text(json.dumps({
        "status": "committed",
        "readback": True,
        "cycle_id": "cycle",
        "cycle_round": 4,
        "authorization_id": "old-authorization",
    }), encoding="utf-8")

    class Client:
        def get_post(self, post_id):
            return {"id": post_id, "status": "pending"}

    evidence = cycle_runtime._confirm_round_has_no_write(
        Client(),
        tmp_path,
        [101],
        cycle_id="cycle",
        round_number=4,
        authorization_id="new-authorization",
    )
    assert evidence is not None
    assert evidence["historical_journal_ids"] == [101]


def test_legacy_round_reconciles_per_post_and_blocks_uncorrelated_running_journal(
    tmp_path, monkeypatch
):
    cycle_id = "mixed-legacy-cycle"
    selected_ids = [115592, 115629, 115282, 115631, 115530]
    store = cycle_runtime.CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id=cycle_id)
    state.phase = "DRAINING"
    state.rounds = 3
    state.seen_post_ids = [1, 2, 3, *selected_ids]
    state.attempts_by_post = {1: 1, 2: 1, 3: 1}
    state.stagnant_rounds = 3
    store.save(state, event="seed_draining")
    store.start_round(
        state,
        round_number=4,
        request={
            "round_number": 4,
            "batch_size": 5,
            "max_new_posts": 0,
            "seen_post_ids": [1, 2, 3, *selected_ids],
            "excluded_post_ids": [],
            "attempts_by_post": {1: 1, 2: 1, 3: 1},
        },
    )
    store.authorize_round(state, selected_ids)

    committed_state = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
    )
    pending_state = WorkState(state=LifecycleState.PENDING, phase=Phase.MEDIA)
    posts = {
        post_id: {
            "id": post_id,
            "status": "pending",
            "title": {"raw": str(post_id)},
            "content": {"raw": "<p>conteudo</p>"},
            "meta": {"_hermes_work_state": json.dumps(
                (committed_state if post_id in {115592, 115629} else pending_state).to_dict()
            )},
        }
        for post_id in selected_ids
    }
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    for post_id in (115592, 115629):
        (journal_dir / f"{post_id}.json").write_text(json.dumps({
            "status": "committed",
            "readback": True,
            "cycle_id": cycle_id,
            "cycle_round": 4,
            "state": committed_state.to_dict(),
        }), encoding="utf-8")
    (journal_dir / "115282.json").write_text(json.dumps({
        "status": "running",
        "post_id": 115282,
        "state": pending_state.to_dict(),
    }), encoding="utf-8")
    for post_id, round_number in ((115631, 3), (115530, 1)):
        (journal_dir / f"{post_id}.json").write_text(json.dumps({
            "status": "committed",
            "readback": True,
            "cycle_id": cycle_id,
            "cycle_round": round_number,
            "state": pending_state.to_dict(),
        }), encoding="utf-8")

    class Client:
        def list_pending(self, *, page, per_page, status):
            return []

        def get_post(self, post_id):
            return posts[post_id]

    calls = []

    def fake_run_v2(_client, _config, _root, **kwargs):
        calls.append(kwargs)
        if not kwargs.get("preview"):
            raise AssertionError("reconciliation must not enter write mode")
        if kwargs.get("expected_selected_ids"):
            return {
                "preview": True,
                "selection_mismatch": True,
                "actual_selected_ids": [],
                "queue": {"selected_ids": [], "eligible_ids": []},
            }
        return {"preview": True, "queue": {"selected_ids": []}}

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    report = cycle_runtime.run_v2_cycle(Client(), _config(), tmp_path)

    assert report.stop_reason == "recovery_uncertain"
    assert calls and all(call["preview"] is True for call in calls)
    assert report.rounds == 3
    assert report.attempts_total == 3
    blocked = report.round_reports[0]
    assert blocked["event"] == "round_reconciliation_blocked"
    assert blocked["reconciled_post_ids"] == [115592, 115629, 115631, 115530]
    assert blocked["unresolved_posts"] == [
        {"post_id": 115282, "reason": "journal_uncorrelated"}
    ]
    persisted = cycle_runtime.CycleStateStore(tmp_path).load()
    assert persisted.pending_round["preview_selected_ids"] == selected_ids


def test_fully_reconciled_round_closes_authorization_without_replay(
    tmp_path, monkeypatch
):
    cycle_id = "fully-reconciled-cycle"
    selected_ids = [201, 202, 203, 204, 205]
    store = cycle_runtime.CycleStateStore(tmp_path)
    state = store.create({"ready_target": 5}, cycle_id=cycle_id)
    state.phase = "DRAINING"
    state.rounds = 3
    state.seen_post_ids = [1, 2, 3, *selected_ids]
    state.attempts_by_post = {1: 1, 2: 1, 3: 1}
    state.stagnant_rounds = 3
    store.save(state, event="seed_draining")
    store.start_round(
        state,
        round_number=4,
        request={
            "round_number": 4,
            "batch_size": 5,
            "max_new_posts": 0,
            "seen_post_ids": [1, 2, 3, *selected_ids],
            "excluded_post_ids": [],
            "attempts_by_post": {1: 1, 2: 1, 3: 1},
        },
    )
    store.authorize_round(state, selected_ids)
    authorization_id = state.pending_round["authorization_id"]

    ready_state = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
    )
    posts = {
        post_id: {
            "id": post_id,
            "status": "pending",
            "title": {"raw": str(post_id)},
            "content": {"raw": "<p>conteudo</p>"},
            "meta": {"_hermes_work_state": json.dumps(ready_state.to_dict())},
        }
        for post_id in selected_ids
    }
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    for post_id in selected_ids:
        (journal_dir / f"{post_id}.json").write_text(json.dumps({
            "status": "committed",
            "readback": True,
            "cycle_id": cycle_id,
            "cycle_round": 4,
            "authorization_id": authorization_id,
            "state": ready_state.to_dict(),
        }), encoding="utf-8")

    class Client:
        def list_pending(self, *, page, per_page, status):
            return []

        def get_post(self, post_id):
            return posts[post_id]

    calls = []

    def fake_run_v2(_client, _config, _root, **kwargs):
        calls.append(kwargs)
        if kwargs.get("expected_selected_ids"):
            return {
                "preview": True,
                "selection_mismatch": False,
                "actual_selected_ids": list(kwargs["expected_selected_ids"]),
                "queue": {
                    "selected_ids": list(kwargs["expected_selected_ids"]),
                    "eligible_ids": list(kwargs["expected_selected_ids"]),
                },
            }
        return {"preview": True, "queue": {"selected_ids": []}}

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    report = cycle_runtime.run_v2_cycle(Client(), _config(), tmp_path)

    assert report.stop_reason == "ready_target"
    assert report.rounds == 4
    assert report.ready_count == 5
    assert report.ready_post_ids == selected_ids
    assert report.attempts_total == 8
    assert report.attempts_by_post == {
        1: 1, 2: 1, 3: 1, 201: 1, 202: 1, 203: 1, 204: 1, 205: 1
    }
    assert calls and all(call["preview"] is True for call in calls)
    reconciled = report.round_reports[0]
    assert reconciled["event"] == "round_reconciled_without_replay"
    assert reconciled["counted_executed_ids"] == selected_ids
    assert reconciled["ready_post_ids"] == selected_ids
    assert [detail["outcome"] for detail in reconciled["post_results"]] == [
        "ready"
    ] * 5
    persisted = cycle_runtime.CycleStateStore(tmp_path).load()
    assert persisted.pending_round is None
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "work" / "v2-cycle" / "journal.jsonl").read_text().splitlines()
    ]
    assert "round_reconciled" in events
    assert "round_completed" not in events


def test_no_write_confirmation_blocks_when_media_registry_has_partial_effect(tmp_path):
    registry_path = tmp_path / "work" / "media_registry.sqlite"
    registry_path.parent.mkdir(parents=True)
    with sqlite3.connect(registry_path) as db:
        db.execute("CREATE TABLE media_runs (post_id INTEGER, created_at TEXT)")
        db.execute("CREATE TABLE media_decisions (post_id INTEGER, created_at TEXT)")
        db.execute("CREATE TABLE media_usage (post_id INTEGER, applied_at TEXT)")
        db.execute(
            "INSERT INTO media_runs(post_id, created_at) VALUES (?, ?)",
            (115329, "2026-10-21 16:00:00"),
        )

    class Client:
        def get_post(self, post_id):
            return {"id": post_id, "status": "pending"}

    assert cycle_runtime._confirm_round_has_no_write(
        Client(),
        tmp_path,
        [115329],
        cycle_id="cycle",
        round_number=4,
    ) is None


@pytest.mark.parametrize("selection_mismatch", [False, True])
def test_invalid_committed_round_is_reconciled_without_reauthorizing_or_recounting(
    tmp_path, monkeypatch, selection_mismatch
):
    cycle_id = "historical-cycle"
    store = cycle_runtime.CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id=cycle_id)
    state.phase = "DRAINING"
    state.rounds = 3
    state.seen_post_ids = [1, 2, 3]
    state.attempts_by_post = {1: 1, 2: 1, 3: 1}
    state.stagnant_rounds = 3
    store.save(state, event="seed_draining")
    store.start_round(
        state,
        round_number=4,
        request={
            "round_number": 4,
            "batch_size": 2,
            "max_new_posts": 0,
            "seen_post_ids": [1, 2, 3],
            "excluded_post_ids": [],
            "attempts_by_post": {1: 1, 2: 1, 3: 1},
        },
    )
    store.authorize_round(state, [115329, 115288])
    authorization_id = state.pending_round["authorization_id"]

    ready_state = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
    )
    retry_state = WorkState(state=LifecycleState.PENDING, phase=Phase.MEDIA)
    posts = {
        115329: {
            "id": 115329,
            "status": "pending",
            "title": {"raw": "A"},
            "content": {"raw": "<p>a</p>"},
            "meta": {"_hermes_work_state": json.dumps(ready_state.to_dict())},
        },
        115288: {
            "id": 115288,
            "status": "pending",
            "title": {"raw": "B"},
            "content": {"raw": "<p>b</p>"},
            "meta": {"_hermes_work_state": json.dumps(retry_state.to_dict())},
        },
    }
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    for post_id, journal_state in ((115329, ready_state), (115288, retry_state)):
        (journal_dir / f"{post_id}.json").write_text(json.dumps({
            "status": "committed",
            "readback": True,
            "cycle_id": cycle_id,
            "cycle_round": 4,
            "authorization_id": authorization_id,
            "state": journal_state.to_dict(),
            "media_readback_status": "not_attempted",
        }), encoding="utf-8")

    class Client:
        def list_pending(self, *, page, per_page, status):
            return list(posts.values()) if page == 1 else []

        def get_post(self, post_id):
            return posts[post_id]

    calls = []

    def fake_run_v2(_client, _config, _root, **kwargs):
        calls.append(kwargs)
        if kwargs.get("expected_selected_ids"):
            return {
                "preview": True,
                "queue": {
                    "selected_ids": [] if selection_mismatch else [115329, 115288]
                },
                "selection_mismatch": selection_mismatch,
            }
        return {"preview": True, "queue": {"selected_ids": []}}

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    report = cycle_runtime.run_v2_cycle(Client(), _config(), tmp_path)

    assert report.stop_reason == "no_eligible_work"
    assert report.rounds == 3
    assert report.attempts_by_post == {1: 1, 2: 1, 3: 1}
    assert len(calls) == 2
    assert all(call["preview"] is True for call in calls)
    persisted = cycle_runtime.CycleStateStore(tmp_path).load()
    assert persisted.pending_round is None
    assert any(
        report_item.get("event") == "round_reconciled_out_of_budget"
        for report_item in report.round_reports
    )
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "work" / "v2-cycle" / "journal.jsonl").read_text().splitlines()
    ]
    assert "round_reconciled_out_of_budget" in events
    assert "round_completed" not in events
