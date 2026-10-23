import json
import sqlite3
from types import SimpleNamespace

import pytest

from unicornio_editor.pipeline_v2 import cycle_runtime
from unicornio_editor.pipeline_v2.cycle_state import CycleState, CycleStateStore
from unicornio_editor.pipeline_v2.lock import RunSessionLock
from unicornio_editor.pipeline_v2.model import LifecycleState, Phase, WorkState
from unicornio_editor.locking import LockError, LockManager


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


def _empty_media_registry(root):
    path = root / "work" / "media_registry.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE media_runs (
                run_id TEXT, post_id INTEGER, planned_deficits TEXT,
                acquisition_outcome TEXT, created_at TEXT
            );
            CREATE TABLE media_decisions (
                run_id TEXT, post_id INTEGER, candidate_id TEXT,
                disposition TEXT, reason_code TEXT, scope TEXT, created_at TEXT
            );
            CREATE TABLE media_usage (
                post_id INTEGER, wordpress_media_id INTEGER,
                status TEXT, applied_at TEXT
            );
            """
        )


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




def test_reconcile_interrupted_pre_writer_as_retry_without_replay(tmp_path):
    _empty_media_registry(tmp_path)
    class Client:
        def list_pending(self, *, page, per_page, status):
            return [{
                "id": 115676, "status": "pending", "title": {"raw": "A"},
                "content": {"raw": "<p>a</p>"},
                "meta": {"_hermes_work_state": json.dumps(
                    WorkState(state=LifecycleState.PENDING, phase=Phase.MEDIA,
                              relevance_approved=True).to_dict()
                )},
            }] if page == 1 else []

        def get_post(self, post_id):
            return self.list_pending(page=1, per_page=1, status="pending")[0]

    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "115676.json").write_text(json.dumps({
        "status": "interrupted_pre_writer", "resumable": True,
        "readback": False, "writer_started": False,
        "run_id": "run-old", "cycle_id": "cycle-test", "cycle_round": 1,
        "authorization_id": "auth-test",
        "state": WorkState(state=LifecycleState.PENDING, phase=Phase.MEDIA,
                            relevance_approved=True).to_dict(),
    }), encoding="utf-8")
    result = cycle_runtime._reconcile_completed_round(
        Client(), tmp_path, [115676], expected_cycle_id="cycle-test",
        expected_cycle_round=1, expected_authorization_id="auth-test",
    )
    assert result is not None
    assert result.reconciliation_blocked is False
    assert result.executed_ids == (115676,)
    assert result.reconciled_without_replay is True
    assert result.details[0]["outcome"] == "retry"
    assert result.details[0]["recovery_status"] == "interrupted_pre_writer"


def test_cycle_recovery_reports_post_lock_contention_without_rewriting_journal(tmp_path):
    post_id = 115676
    _empty_media_registry(tmp_path)
    state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        relevance_approved=True,
    )
    journal_path = tmp_path / "work" / "v2-journal" / f"{post_id}.json"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(json.dumps({
        "status": "running",
        "post_id": post_id,
        "run_id": "interrupted-run",
        "cycle_id": "cycle-test",
        "cycle_round": 1,
        "authorization_id": "auth-test",
        "protocol_version": 2,
        "writer_started": False,
        "state": state.to_dict(),
    }), encoding="utf-8")

    class Client:
        def list_pending(self, *, page, per_page, status):
            return [{
                "id": post_id,
                "status": "pending",
                "title": {"raw": "A"},
                "content": {"raw": "<p>A</p>"},
                "meta": {"_hermes_work_state": json.dumps(state.to_dict())},
            }] if page == 1 else []

        def get_post(self, requested_id):
            assert int(requested_id) == post_id
            return self.list_pending(page=1, per_page=1, status="pending")[0]

    held = LockManager(tmp_path / "work" / "locks").acquire(post_id)
    held.__enter__()
    try:
        result = cycle_runtime._reconcile_completed_round(
            Client(), tmp_path, [post_id], expected_cycle_id="cycle-test",
            expected_cycle_round=1, expected_authorization_id="auth-test",
        )
    finally:
        held.__exit__(None, None, None)

    assert result is not None
    assert result.locked is True
    assert result.reconciliation_blocking_info["locked_post_id"] == post_id
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == "running"


def test_reconcile_interrupted_pre_writer_with_media_effects_stays_blocked(tmp_path):
    state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        relevance_approved=True,
    )

    class Client:
        def list_pending(self, *, page, per_page, status):
            return [{
                "id": 115633,
                "status": "pending",
                "title": {"raw": "A"},
                "content": {"raw": "<p>a</p>"},
                "meta": {"_hermes_work_state": json.dumps(state.to_dict())},
            }] if page == 1 else []

        def get_post(self, post_id):
            return self.list_pending(page=1, per_page=1, status="pending")[0]

    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "115633.json").write_text(json.dumps({
        "status": "interrupted_pre_writer", "resumable": True,
        "readback": False, "writer_started": False,
        "run_id": "run-media", "cycle_id": "cycle-test", "cycle_round": 4,
        "authorization_id": "auth-test", "state": state.to_dict(),
    }), encoding="utf-8")
    registry_path = tmp_path / "work" / "media_registry.sqlite"
    with sqlite3.connect(registry_path) as db:
        db.execute(
            """CREATE TABLE media_decisions (
                run_id TEXT, candidate_id TEXT, disposition TEXT,
                reason_code TEXT, scope TEXT
            )"""
        )
        db.execute(
            """INSERT INTO media_decisions
               (run_id, candidate_id, disposition, reason_code, scope)
               VALUES (?, ?, ?, ?, ?)""",
            ("run-media", "candidate-1", "accepted", "candidate_accepted", "candidate"),
        )

    result = cycle_runtime._reconcile_completed_round(
        Client(), tmp_path, [115633], expected_cycle_id="cycle-test",
        expected_cycle_round=4, expected_authorization_id="auth-test",
    )

    assert result is not None
    assert result.reconciliation_blocked is True
    assert result.executed_ids == ()
    assert result.reconciliation_blocking_info["unresolved_posts"] == [{
        "post_id": 115633,
        "reason": "media_effects_pre_writer",
        "recovery_classification": "MEDIA_EFFECTS_PRE_WRITER",
        "media_effect_evidence": {
            "media_registry_checked": True,
            "effects_detected": True,
            "evidence_status": "evidence_unavailable",
            "reasons": ["media_registry_incomplete", "accepted_media_decision"],
            "accepted_media_ids": [],
            "applied_media_ids": [],
            "accepted_decision_count": 1,
        },
    }]



def test_reconcile_completed_round_separates_journal_from_another_cycle(tmp_path):
    _empty_media_registry(tmp_path)
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


def test_historical_journals_reconcile_only_with_complete_no_effect_evidence(tmp_path):
    _empty_media_registry(tmp_path)
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    cycle_journal = tmp_path / "work" / "v2-cycle" / "journal.jsonl"
    cycle_journal.parent.mkdir(parents=True, exist_ok=True)
    cycle_journal.write_text(json.dumps({
        "event": "round_authorized",
        "cycle_id": "current-cycle",
        "round_number": 4,
        "authorization_id": "current-authorization",
        "at": "2026-10-22T10:00:00+00:00",
    }) + "\n", encoding="utf-8")
    with sqlite3.connect(tmp_path / "work" / "media_registry.sqlite") as db:
        db.executemany(
            "INSERT INTO media_runs(run_id, post_id, created_at) VALUES (?, ?, ?)",
            [
                ("historical-run-115668", 115668, "2026-10-21 10:00:00"),
                ("historical-run-115666", 115666, "2026-10-21 11:00:00"),
            ],
        )
    for post_id in (115668, 115666):
        (journal_dir / f"{post_id}.json").write_text(json.dumps({
            "status": "committed",
            "readback": True,
            "run_id": f"historical-run-{post_id}",
            "state": WorkState(
                state=LifecycleState.PENDING,
                phase=Phase.MEDIA,
            ).to_dict(),
        }), encoding="utf-8")

    class Client:
        def list_pending(self, *, page, per_page, status):
            return []

        def get_post(self, post_id):
            return {"id": post_id, "status": "pending"}

    result = cycle_runtime._reconcile_completed_round(
        Client(), tmp_path, [115668, 115666],
        expected_cycle_id="current-cycle",
        expected_cycle_round=4,
        expected_authorization_id="current-authorization",
    )

    assert result is not None
    assert result.reconciliation_blocked is False
    assert result.reconciled_without_replay is True
    assert result.executed_ids == ()
    assert [item["post_id"] for item in result.details] == [115668, 115666]
    assert all(item["outcome"] == "not_executed" for item in result.details)


def test_mixed_round_reconciles_committed_posts_before_live_pre_writer_journal(
    tmp_path,
):
    _empty_media_registry(tmp_path)
    ready_state = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
    )
    pending_state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        relevance_approved=True,
    )
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "101.json").write_text(json.dumps({
        "status": "committed",
        "readback": True,
        "cycle_id": "current-cycle",
        "cycle_round": 4,
        "authorization_id": "current-auth",
        "state": ready_state.to_dict(),
    }), encoding="utf-8")
    (journal_dir / "102.json").write_text(json.dumps({
        "status": "running",
        "post_id": 102,
        "run_id": "live-run",
        "cycle_id": "current-cycle",
        "cycle_round": 4,
        "authorization_id": "current-auth",
        "protocol_version": 2,
        "writer_started": False,
        "state": pending_state.to_dict(),
    }), encoding="utf-8")

    class Client:
        def list_pending(self, *, page, per_page, status):
            return [self.get_post(101), self.get_post(102)] if page == 1 else []

        def get_post(self, post_id):
            state = ready_state if int(post_id) == 101 else pending_state
            return {
                "id": int(post_id),
                "status": "pending",
                "meta": {"_hermes_work_state": json.dumps(state.to_dict())},
            }

    result = cycle_runtime._reconcile_completed_round(
        Client(), tmp_path, [101, 102],
        expected_cycle_id="current-cycle",
        expected_cycle_round=4,
        expected_authorization_id="current-auth",
    )

    assert result is not None
    assert result.reconciliation_blocked is False
    assert result.reconciled_without_replay is True
    assert result.executed_ids == (101, 102)
    details = {item["post_id"]: item for item in result.details}
    assert details[101]["outcome"] == "ready"
    assert details[102]["outcome"] == "retry"
    assert details[102]["recovery_status"] == "interrupted_pre_writer"
    assert json.loads((journal_dir / "102.json").read_text())["status"] == "interrupted_pre_writer"


def test_resume_five_post_authorized_round_reconciles_recovered_post_without_replay(tmp_path, monkeypatch):
    _empty_media_registry(tmp_path)
    post_ids = [115641, 115598, 115676, 115631, 115633]
    states = {
        115641: ("retry", "featured_vision"),
        115598: ("human_required", "inline_missing"),
        115676: ("interrupted_pre_writer", "process_interrupted"),
        115631: ("historical", ""),
        115633: ("historical", ""),
    }
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    for post_id in post_ids:
        kind, failure_class = states[post_id]
        journal_state = WorkState(
            state=(
                LifecycleState.HUMAN_REQUIRED
                if post_id == 115598
                else LifecycleState.PENDING
            ),
            phase=Phase.MEDIA,
            relevance_approved=True,
        )
        payload = {
            "cycle_id": "old-cycle" if post_id in {115631, 115633} else "929621ed4ea945ed9ecc560d3efb9b90",
            "cycle_round": 1,
            "authorization_id": "old-auth" if post_id in {115631, 115633} else "auth-929621",
            "state": journal_state.to_dict(),
        }
        if post_id == 115676:
            payload.update({
                "status": kind, "resumable": True, "writer_started": False,
                "readback": False, "failure_class": failure_class,
            })
        elif post_id in {115631, 115633}:
            payload.update({"status": "historical", "readback": False})
        else:
            payload.update({"status": "committed", "readback": True})
            if post_id == 115641:
                payload.update({
                    "media_readback_status": "confirmed",
                    "media_application_status": "confirmed",
                    "html_readback_status": "confirmed",
                    "apply_media_records": [{"readback_status": "confirmed"}],
                })
        (journal_dir / f"{post_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    class Client:
        def list_pending(self, *, page, per_page, status):
            return [self.get_post(post_id) for post_id in post_ids] if page == 1 else []

        def get_post(self, post_id):
            state = WorkState(
                state=(
                    LifecycleState.HUMAN_REQUIRED
                    if post_id == 115598
                    else LifecycleState.PENDING
                ),
                phase=Phase.MEDIA,
                relevance_approved=True,
            )
            return {
                "id": post_id,
                "status": "pending",
                "meta": {"_hermes_work_state": json.dumps(state.to_dict())},
            }

    monkeypatch.setattr(
        cycle_runtime,
        "_post_no_write_evidence",
        lambda *args, **kwargs: {"post_id": int(args[2]), "media_registry_current_round_rows": 0},
    )
    result = cycle_runtime._reconcile_completed_round(
        Client(), tmp_path, post_ids,
        expected_cycle_id="929621ed4ea945ed9ecc560d3efb9b90",
        expected_cycle_round=1,
        expected_authorization_id="auth-929621",
    )
    assert result is not None
    assert result.reconciliation_blocked is False
    assert result.reconciled_without_replay is True
    detail = {item["post_id"]: item for item in result.details}
    assert detail[115676]["outcome"] == "retry"
    assert detail[115676]["write_replayed"] is False
    assert detail[115676]["recovery_status"] == "interrupted_pre_writer"
    assert detail[115676]["wordpress_state_readback"] is True
    assert detail[115676]["write_started"] is False
    assert detail[115676]["transaction_readback"] is False
    assert detail[115676]["write_readback_confirmed"] is False
    assert detail[115598]["outcome"] == "human_required"
    assert detail[115641]["journal_status"] == "committed"
    assert detail[115641]["journal_transaction_readback"] is True
    assert detail[115641]["wordpress_post_status"] == "pending"
    assert detail[115641]["wordpress_v2_state"] == "pending"
    assert detail[115641]["media_application_status"] == "confirmed"
    assert detail[115641]["html_readback_status"] == "confirmed"
    assert 115676 in result.executed_ids
    assert len(result.details) == len(post_ids)


def test_resume_five_post_round_through_cycle_updates_checkpoint_without_writes(
    tmp_path, monkeypatch
):
    _empty_media_registry(tmp_path)
    cycle_id = "resume-five-post-cycle"
    post_ids = [115641, 115598, 115676, 115631, 115633]
    store = cycle_runtime.CycleStateStore(tmp_path)
    cycle_config = cycle_runtime.CycleConfig(
        ready_target=5,
        new_batch_size=5,
        max_unique_posts=5,
        max_total_attempts=20,
        max_attempts_per_post_per_cycle=2,
        max_stagnant_rounds=2,
        max_drain_rounds=2,
        max_duration_seconds=60,
    )
    state = store.create(cycle_config.__dict__, cycle_id=cycle_id)
    state.phase = "DRAINING"
    state.rounds = 3
    state.seen_post_ids = [1, 2, 3, *post_ids]
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
            "seen_post_ids": [1, 2, 3, *post_ids],
            "excluded_post_ids": [],
            "attempts_by_post": {1: 1, 2: 1, 3: 1},
        },
    )
    store.authorize_round(state, post_ids)
    authorization_id = state.pending_round["authorization_id"]

    states = {
        115641: WorkState(state=LifecycleState.PENDING, phase=Phase.MEDIA, relevance_approved=True),
        115598: WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA, relevance_approved=True),
        115676: WorkState(state=LifecycleState.PENDING, phase=Phase.MEDIA, relevance_approved=True),
        115631: WorkState(state=LifecycleState.PENDING, phase=Phase.MEDIA, relevance_approved=True),
        115633: WorkState(state=LifecycleState.PENDING, phase=Phase.MEDIA, relevance_approved=True),
    }
    posts = {
        post_id: {
            "id": post_id,
            "status": "pending",
            "title": {"raw": str(post_id)},
            "content": {"raw": "<p>conteudo</p>"},
            "meta": {"_hermes_work_state": json.dumps(work_state.to_dict())},
        }
        for post_id, work_state in states.items()
    }
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    for post_id in post_ids:
        work_state = states[post_id]
        current = post_id not in {115631, 115633}
        payload = {
            "status": "committed" if current else "historical",
            "readback": current,
            "cycle_id": cycle_id if current else "old-cycle",
            "cycle_round": 4 if current else 2,
            "authorization_id": authorization_id if current else "old-auth",
            "state": work_state.to_dict(),
        }
        if post_id == 115676:
            payload.update({
                "status": "interrupted_pre_writer",
                "resumable": True,
                "writer_started": False,
                "readback": False,
                "failure_class": "process_interrupted",
            })
        (journal_dir / f"{post_id}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    class Client:
        def list_pending(self, *, page, per_page, status):
            return list(posts.values()) if page == 1 else []

        def get_post(self, post_id):
            return posts[post_id]

    monkeypatch.setattr(
        cycle_runtime,
        "_post_no_write_evidence",
        lambda _client, _root, post_id, **_kwargs: {
            "post_id": int(post_id),
            "media_registry_current_round_rows": 0,
        },
    )
    calls = []

    def fake_run_v2(_client, _config, _root, **kwargs):
        calls.append(kwargs)
        if kwargs.get("expected_selected_ids"):
            return {
                "preview": True,
                "selection_mismatch": False,
                "queue": {"selected_ids": list(post_ids)},
            }
        return {"preview": True, "queue": {"selected_ids": []}}

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    report = cycle_runtime.run_v2_cycle(
        Client(), SimpleNamespace(), tmp_path, cycle_config=cycle_config
    )

    assert report.stop_reason == "no_eligible_work"
    assert report.rounds == 4
    assert report.ready_count == 0
    assert report.attempts_by_post == {1: 1, 2: 1, 3: 1, 115641: 1, 115598: 1, 115676: 1}
    assert all(call["preview"] is True for call in calls)
    assert len(calls) == 2

    reconciled = next(
        item for item in report.round_reports
        if item.get("event") == "round_reconciled_without_replay"
    )
    assert [detail["outcome"] for detail in reconciled["post_results"]] == [
        "retry", "human_required", "retry", "not_executed", "not_executed"
    ]
    assert reconciled["transaction_readback_post_ids"] == [115641, 115598]
    assert reconciled["state_readback_only_post_ids"] == [115676, 115631, 115633]
    recovered = {
        detail["post_id"]: detail for detail in reconciled["post_results"]
    }[115676]
    assert recovered["wordpress_state_readback"] is True
    assert recovered["write_started"] is False
    assert recovered["transaction_readback"] is False

    persisted = store.load()
    assert persisted.status == "completed"
    assert persisted.pending_round is None
    assert persisted.rounds == 4


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
    assert result["status"] == "skipped"
    assert result["stop_reason"] == "skipped_locked"
    telemetry = (tmp_path / "work" / "telemetry.jsonl").read_text().splitlines()
    assert json.loads(telemetry[-1])["event"] == "v2_cycle_skipped_locked"
    assert not (tmp_path / "work" / "v2-cycle" / "state.json").exists()


def test_cycle_executor_lock_skip_preserves_resumable_checkpoint(tmp_path, monkeypatch):
    store = cycle_runtime.CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="executor-locked")
    store.save(state, event="seed")

    monkeypatch.setattr(
        cycle_runtime,
        "run_v2",
        lambda *_args, **kwargs: (
            {"preview": True, "queue": {"selected_ids": [101]}}
            if kwargs.get("preview")
            else {
                "locked": True,
                "locked_post_id": 101,
                "lock_error": "post 101 is already locked",
            }
        ),
    )
    report = cycle_runtime.run_v2_cycle(object(), _config(), tmp_path)

    assert report.stop_reason == "skipped_locked"
    assert report.phase == "ADMISSION"
    persisted = store.load()
    assert persisted.status == "running"
    assert persisted.pending_round is not None
    assert persisted.pending_round["preview_selected_ids"] == [101]
    assert persisted.round_reports[0]["event"] == "cycle_skipped_locked"
    assert persisted.round_reports[0]["locked_post_id"] == 101
    assert persisted.round_reports[0]["reason"] == "post_lock_busy"


def test_unadmitted_round_reselects_around_busy_post_before_authorizing(tmp_path, monkeypatch):
    calls = []
    def fake_run_v2(_client, _config_value, _root, **kwargs):
        calls.append(kwargs)
        if kwargs.get("preview"):
            excluded = set(kwargs.get("excluded_post_ids") or ())
            selected = [101, 103] if 102 in excluded else [101, 102]
            return {"preview": True, "queue": {"selected_ids": selected}}
        assert kwargs["expected_selected_ids"] == [101, 103]
        assert kwargs["excluded_post_ids"] == (102,)
        assert set(kwargs["reserved_post_locks"]) == {101, 103}
        pending = cycle_runtime.CycleStateStore(_root).load().pending_round
        assert pending["request"]["excluded_post_ids"] == [102]
        for handle in kwargs["reserved_post_locks"].values():
            handle.assert_owned()
        return {"details": [_ready_detail(101), _ready_detail(103)]}

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    manager = LockManager(tmp_path / "work" / "locks")
    occupied = manager.acquire(102)
    occupied.__enter__()
    try:
        report = cycle_runtime.run_v2_cycle(
            object(),
            _config(),
            tmp_path,
            cycle_config=cycle_runtime.CycleConfig(
                ready_target=2,
                new_batch_size=2,
                max_unique_posts=5,
                max_total_attempts=10,
                max_attempts_per_post_per_cycle=2,
                max_stagnant_rounds=2,
                max_drain_rounds=2,
                max_duration_seconds=60,
            ),
        )
    finally:
        occupied.__exit__(None, None, None)

    assert report.stop_reason == "ready_target"
    assert report.ready_post_ids == [101, 103]
    assert [call["preview"] for call in calls] == [True, True, False]
    assert calls[1]["excluded_post_ids"] == {102}
    for post_id in (101, 103):
        released = manager.acquire(post_id)
        released.release()


def test_authorized_round_keeps_original_ids_when_one_post_lock_is_busy(tmp_path, monkeypatch):
    store = cycle_runtime.CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="authorized-lock")
    store.start_round(
        state,
        round_number=1,
        request={
            "round_number": 1,
            "batch_size": 2,
            "max_new_posts": 2,
            "seen_post_ids": [],
            "excluded_post_ids": [],
            "attempts_by_post": {},
        },
    )
    store.authorize_round(state, [101, 102])
    calls = []

    def fake_run_v2(_client, _config_value, _root, **kwargs):
        calls.append(kwargs)
        if kwargs.get("preview"):
            return {"preview": True, "queue": {"selected_ids": [101, 102]}}
        raise AssertionError("an authorized locked round must not enter write mode")

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    manager = LockManager(tmp_path / "work" / "locks")
    occupied = manager.acquire(102)
    occupied.__enter__()
    try:
        report = cycle_runtime.run_v2_cycle(object(), _config(), tmp_path)
    finally:
        occupied.__exit__(None, None, None)

    persisted = store.load()
    assert report.stop_reason == "skipped_locked"
    assert [call["preview"] for call in calls] == [True]
    assert persisted.pending_round["preview_selected_ids"] == [101, 102]
    assert persisted.round_reports[0]["locked_post_id"] == 102
    assert persisted.round_reports[0]["authorization_preserved"] is True


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
    _empty_media_registry(tmp_path)
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
    _empty_media_registry(tmp_path)
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
    _empty_media_registry(tmp_path)
    CycleStateStore(tmp_path).save(
        CycleState(cycle_id="cycle"),
        event="round_authorized",
        round_number=4,
        authorization_id="new-authorization",
        selected_ids=[101],
    )
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


def test_no_write_proof_uses_exact_authorization_timestamp(tmp_path):
    _empty_media_registry(tmp_path)
    store = CycleStateStore(tmp_path)
    store.save(
        CycleState(cycle_id="cycle"),
        event="round_authorized",
        round_number=4,
        authorization_id="current-auth",
        selected_ids=[101],
        at="2026-10-23T11:00:00+00:00",
    )
    store.save(
        CycleState(cycle_id="cycle"),
        event="round_authorized",
        round_number=4,
        authorization_id="other-auth",
        selected_ids=[101],
        at="2026-10-23T12:00:00+00:00",
    )
    with sqlite3.connect(tmp_path / "work" / "media_registry.sqlite") as db:
        db.execute(
            "INSERT INTO media_runs(run_id, post_id, created_at) VALUES (?, ?, ?)",
            ("run-after-current-auth", 101, "2026-10-23 11:30:00"),
        )

    class Client:
        def get_post(self, post_id):
            return {"id": post_id, "status": "pending"}

    assert cycle_runtime._round_authorized_at(
        tmp_path,
        "cycle",
        4,
        authorization_id="current-auth",
    ) == "2026-10-23T11:00:00+00:00"
    assert cycle_runtime._post_no_write_evidence(
        Client(),
        tmp_path,
        101,
        cycle_id="cycle",
        round_number=4,
        authorization_id="current-auth",
    ) is None


def test_no_write_proof_blocks_registry_rows_without_timestamps(tmp_path):
    _empty_media_registry(tmp_path)
    CycleStateStore(tmp_path).save(
        CycleState(cycle_id="cycle"),
        event="round_authorized",
        round_number=4,
        authorization_id="current-auth",
        selected_ids=[101],
    )
    with sqlite3.connect(tmp_path / "work" / "media_registry.sqlite") as db:
        db.execute(
            "INSERT INTO media_usage(post_id, status, applied_at) VALUES (?, ?, NULL)",
            (101, "accepted"),
        )

    class Client:
        def get_post(self, post_id):
            return {"id": post_id, "status": "pending"}

    assert cycle_runtime._post_no_write_evidence(
        Client(),
        tmp_path,
        101,
        cycle_id="cycle",
        round_number=4,
        authorization_id="current-auth",
    ) is None


def test_legacy_round_reconciles_per_post_and_blocks_uncorrelated_running_journal(
    tmp_path, monkeypatch
):
    _empty_media_registry(tmp_path)
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
