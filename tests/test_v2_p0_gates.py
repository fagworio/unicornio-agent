import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from unicornio_editor.locking import LockManager
from unicornio_editor.pipeline_v2.cycle import CycleConfig, CycleRoundResult, run_cycle
from unicornio_editor.pipeline_v2.cycle_admin import expire_pre_authorization
from unicornio_editor.pipeline_v2 import cycle_runtime
from unicornio_editor.pipeline_v2.cycle_state import CycleStateStore
from unicornio_editor.pipeline_v2.model import LifecycleState, Phase, WorkState
from unicornio_editor.pipeline_v2.runtime import run_v2
from unicornio_editor.cli import _v2_cycle_exit_code


def _config(root):
    quarantine = root / "config" / "v2-quarantine.json"
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    quarantine.write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )
    return SimpleNamespace(
        v2_historical_cohort_file=None,
        v2_admission_after="2026-10-09T00:00:00Z",
        v2_admission_allowlist=(),
    )


def _post(post_id):
    state = WorkState(state=LifecycleState.PENDING, phase=Phase.RELEVANCE)
    return {
        "id": post_id,
        "status": "pending",
        "date_gmt": "2026-10-09T01:00:00Z",
        "title": {"raw": f"Post {post_id}"},
        "content": {"raw": "<p>Texto editorial em português.</p>"},
        "meta": {"_hermes_work_state": json.dumps(state.to_dict())},
    }


class _Client:
    def __init__(self, posts):
        self.posts = {post["id"]: post for post in posts}
        self.list_calls = 0

    def list_pending(self, *, page, per_page, status):
        self.list_calls += 1
        return list(self.posts.values()) if page == 1 else []

    def get_post(self, post_id):
        return self.posts[post_id]


def test_lock_reselection_reuses_candidate_snapshot_without_wordpress_query(tmp_path):
    client = _Client([_post(101), _post(102)])
    config = _config(tmp_path)
    first = run_v2(
        client,
        config,
        tmp_path,
        limit=2,
        preview=True,
        return_candidate_snapshot=True,
    )
    assert first["queue"]["selected_ids"]
    calls_after_first_preview = client.list_calls

    second = run_v2(
        client,
        config,
        tmp_path,
        limit=2,
        preview=True,
        candidate_snapshot=first["_candidate_snapshot"],
        excluded_post_ids=[first["queue"]["selected_ids"][0]],
        return_candidate_snapshot=True,
    )

    assert second["queue"]["selected_ids"]
    assert client.list_calls == calls_after_first_preview


def test_lock_scan_budget_is_separate_from_unique_post_budget_and_reports_counts(
    tmp_path, monkeypatch
):
    locked = set(range(1, 27))
    snapshot = [
        (post_id, {"post": {}, "v2_state": WorkState(state=LifecycleState.PENDING, phase=Phase.RELEVANCE)})
        for post_id in range(1, 27)
    ] + [
        (100, {"post": {}, "v2_state": WorkState(state=LifecycleState.PENDING, phase=Phase.RELEVANCE)})
    ]
    calls = []

    def fake_run_v2(_client, _config, _root, **kwargs):
        excluded = {int(value) for value in kwargs.get("excluded_post_ids", ())}
        selected = next(post_id for post_id, _context in snapshot if post_id not in excluded)
        calls.append((bool(kwargs.get("preview")), selected))
        if kwargs.get("preview"):
            return {"queue": {"selected_ids": [selected]}, "_candidate_snapshot": snapshot}
        return {
            "details": [{
                "post_id": selected,
                "initial_state": "pending",
                "outcome": "ready",
                "readback": True,
                "state": {"state": "ready", "phase": "validate"},
                "progress": True,
            }]
        }

    def fake_reserve(_root, selected_ids, *, lock_ttl):
        post_id = int(selected_ids[0])
        return ({}, post_id, "legacy lock") if post_id in locked else ({}, None, "")

    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    monkeypatch.setattr(cycle_runtime, "_reserve_post_lock_batch", fake_reserve)

    report = cycle_runtime.run_v2_cycle(
        object(), SimpleNamespace(lock_ttl=60), tmp_path,
        cycle_config=CycleConfig(
            ready_target=1,
            new_batch_size=1,
            max_unique_posts=25,
            max_lock_reselections=30,
            max_total_attempts=1,
            max_attempts_per_post_per_cycle=1,
            max_stagnant_rounds=2,
            max_drain_rounds=2,
            max_duration_seconds=60,
        ),
    )

    assert report.stop_reason == "ready_target"
    assert report.ready_post_ids == [100]
    assert report.candidates_examined == 27
    assert report.locked_candidates == 26
    assert report.selected_candidates == 1
    assert report.processed_candidates == 1
    assert report.round_reports[0]["selection_stats"] == {
        "candidates_examined": 27,
        "locked_candidates": 26,
        "selected_candidates": 1,
        "processed_candidates": 1,
    }
    assert len(calls) == 28


def test_thirty_legacy_locks_are_not_modified_or_auto_reconciled(tmp_path):
    lock_root = tmp_path / "work" / "locks"
    lock_root.mkdir(parents=True)
    before = {}
    for post_id in range(186, 216):
        path = lock_root / f"{post_id}.lock"
        payload = json.dumps({"created_at": 1, "released": False})
        path.write_text(payload, encoding="utf-8")
        before[post_id] = path.read_bytes()

    handles, locked_post_id, error = cycle_runtime._reserve_post_lock_batch(
        tmp_path, list(range(186, 216)), lock_ttl=60
    )

    assert handles == {}
    assert locked_post_id == 186
    assert "legacy_unverifiable" in error
    assert all((lock_root / f"{post_id}.lock").read_bytes() == value for post_id, value in before.items())


def test_deadline_expiring_during_lock_reselection_does_not_authorize_or_query_again(
    tmp_path, monkeypatch
):
    calls = []
    snapshot = [(101, {"post": {}, "v2_state": WorkState(state=LifecycleState.PENDING, phase=Phase.RELEVANCE)})]

    def fake_run_v2(_client, _config, _root, **kwargs):
        calls.append(kwargs)
        return {
            "preview": True,
            "queue": {"selected_ids": [101]},
            "_candidate_snapshot": snapshot,
        }

    reserve_calls = iter([
        ({}, 101, "legacy lock"),
    ])
    monkeypatch.setattr(cycle_runtime, "run_v2", fake_run_v2)
    monkeypatch.setattr(cycle_runtime, "_reserve_post_lock_batch", lambda *_a, **_k: next(reserve_calls))
    base_monotonic = time.monotonic()
    monotonic_values = iter([base_monotonic, base_monotonic + 11.0])
    monkeypatch.setattr(cycle_runtime, "monotonic", lambda: next(monotonic_values, 11.0))

    report = cycle_runtime.run_v2_cycle(
        object(),
        SimpleNamespace(lock_ttl=60),
        tmp_path,
        cycle_config=CycleConfig(
            ready_target=1,
            new_batch_size=1,
            max_unique_posts=2,
            max_total_attempts=2,
            max_attempts_per_post_per_cycle=1,
            max_stagnant_rounds=2,
            max_drain_rounds=2,
            max_duration_seconds=10,
        ),
    )

    assert report.stop_reason == "duration_limit", report
    assert len(calls) == 1
    state = CycleStateStore(tmp_path).load()
    assert state.status == "running"
    assert state.pending_round is not None
    assert state.pending_round["preview_selected_ids"] is None


def test_expired_post_deadline_stops_before_queue_read_or_external_work(tmp_path):
    client = _Client([_post(501)])
    result = run_v2(
        client,
        _config(tmp_path),
        tmp_path,
        preview=True,
        deadline=time.monotonic() - 1,
    )

    assert result["budget_violation"] == "duration_limit"
    assert result["details"] == []
    assert client.list_calls == 0


def test_expired_pre_authorization_transition_preserves_elapsed_and_archives_identity(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"max_duration_seconds": 3600}, cycle_id="expired-cycle")
    intent = store.start_round(state, round_number=1, request={"batch_size": 1})
    state.elapsed_seconds = 7874.96
    store.save(state, event="elapsed_budget_reached")
    before = json.loads((tmp_path / "work" / "v2-cycle" / "state.json").read_text())

    preview = expire_pre_authorization(
        tmp_path,
        cycle_id="expired-cycle",
        pending_round=1,
        round_intent_id=intent,
        apply=False,
    )
    assert preview["status"] == "preview"
    assert preview["elapsed_seconds"] == 7874.96
    assert json.loads((tmp_path / "work" / "v2-cycle" / "state.json").read_text()) == before

    applied = expire_pre_authorization(
        tmp_path,
        cycle_id="expired-cycle",
        pending_round=1,
        round_intent_id=intent,
        apply=True,
    )
    assert applied["status"] == "applied"
    current = CycleStateStore(tmp_path).load()
    assert current.pending_round is None
    assert current.status == "completed"
    assert current.phase == "COMPLETED"
    assert current.elapsed_seconds == 7874.96
    assert current.stop_reason == "pre_authorization_expired"
    archived = tmp_path / "work" / "v2-cycle" / "archives" / "preauthorization-expired-expired-cycle-1" / "state.json"
    assert json.loads(archived.read_text())["pending_round"]["round_intent_id"] == intent
    transition = json.loads((tmp_path / "work" / "v2-cycle" / "v2-preauthorization-expiry-transition.json").read_text())
    assert transition["version"] == 1
    assert transition["phase"] == "completed"
    repeated = expire_pre_authorization(
        tmp_path,
        cycle_id="expired-cycle",
        pending_round=1,
        round_intent_id=intent,
        apply=True,
    )
    assert repeated["status"] == "already_applied"
    transition["phase"] = "prepared"
    transition.pop("completed_at", None)
    transition.pop("new_state_sha256", None)
    (tmp_path / "work" / "v2-cycle" / "v2-preauthorization-expiry-transition.json").write_text(
        json.dumps(transition), encoding="utf-8"
    )
    recovered = expire_pre_authorization(
        tmp_path,
        cycle_id="expired-cycle",
        pending_round=1,
        round_intent_id=intent,
        apply=True,
    )
    assert recovered["status"] == "already_applied"
    assert json.loads(
        (tmp_path / "work" / "v2-cycle" / "v2-preauthorization-expiry-transition.json").read_text()
    )["phase"] == "completed"


def test_old_started_registry_operations_block_replay_independent_of_current_journal(tmp_path):
    path = tmp_path / "work" / "media_registry.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE media_runs (run_id TEXT, post_id INTEGER, created_at TEXT);
            CREATE TABLE media_decisions (run_id TEXT, post_id INTEGER, created_at TEXT);
            CREATE TABLE media_usage (post_id INTEGER, status TEXT, applied_at TEXT,
                run_id TEXT, cycle_id TEXT, cycle_round INTEGER, authorization_id TEXT,
                accepted_at TEXT);
            CREATE TABLE media_operations (post_id INTEGER, run_id TEXT, cycle_id TEXT,
                cycle_round INTEGER, authorization_id TEXT, started_at TEXT,
                completed_at TEXT, operation_status TEXT);
            """
        )
        for post_id in (186, 239, 127, 204):
            db.execute(
                "INSERT INTO media_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (post_id, f"old-{post_id}", "old-cycle", 1, "old-auth",
                 "2026-11-01T00:00:00+00:00", None, "started"),
            )

    evidence = cycle_runtime._registry_has_round_evidence(
        tmp_path,
        [186, 239, 127, 204],
        authorized_at="2026-11-05T00:00:00+00:00",
        expected_cycle_id="new-cycle",
        expected_cycle_round=1,
        expected_authorization_id="new-auth",
    )

    assert evidence["proof_of_absence"] is False
    assert evidence["temporal_classification"]["historical_started_operation"] == [186, 239, 127, 204]


def test_old_started_registry_operation_blocks_historical_writer_replay(tmp_path):
    path = tmp_path / "work" / "media_registry.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE media_runs (run_id TEXT, post_id INTEGER, created_at TEXT);
            CREATE TABLE media_decisions (run_id TEXT, post_id INTEGER, created_at TEXT);
            CREATE TABLE media_usage (post_id INTEGER, status TEXT, applied_at TEXT,
                run_id TEXT, cycle_id TEXT, cycle_round INTEGER, authorization_id TEXT,
                accepted_at TEXT);
            CREATE TABLE media_operations (post_id INTEGER, run_id TEXT, cycle_id TEXT,
                cycle_round INTEGER, authorization_id TEXT, started_at TEXT,
                operation_status TEXT);
            INSERT INTO media_operations VALUES
                (186, 'different-run', 'old-cycle', 2, 'old-auth',
                 '2026-11-01T00:00:00+00:00', 'started');
            """
        )
    journal = tmp_path / "work" / "v2-cycle" / "journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps({
            "event": "round_authorized",
            "cycle_id": "new-cycle",
            "round_number": 1,
            "authorization_id": "new-auth",
            "at": "2026-11-05T00:00:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )
    evidence = cycle_runtime._confirm_round_has_no_write(
        SimpleNamespace(get_post=lambda _post_id: {"status": "pending"}),
        tmp_path,
        [186],
        cycle_id="new-cycle",
        round_number=1,
        authorization_id="new-auth",
    )
    assert evidence is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "skipped", "stop_reason": "skipped_locked"}, 75),
        ({"status": "blocked", "stop_reason": "recovery_uncertain"}, 2),
        (SimpleNamespace(stop_reason="ready_target"), 0),
    ],
)
def test_v2_cycle_exit_code_distinguishes_operational_skip_from_success(result, expected):
    assert _v2_cycle_exit_code(result) == expected
