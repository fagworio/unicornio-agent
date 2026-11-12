import json
import sqlite3
import stat
from pathlib import Path

import pytest

import unicornio_editor.pipeline_v2.cycle_state as cycle_state_module
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
        "round_authorization_prepared",
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


def test_round_intent_is_not_authorization_until_selection_is_durable(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="intent-only")
    intent_id = store.start_round(state, round_number=1, request={"batch_size": 1})
    assert state.pending_round["authorization_id"] is None
    assert state.pending_round["preview_selected_ids"] is None
    start_event = json.loads(store.journal_path.read_text().splitlines()[-1])
    assert start_event["event"] == "round_started"
    assert start_event["round_intent_id"] == intent_id
    assert "authorization_id" not in start_event
    with pytest.raises(RuntimeError, match="authorized selection"):
        store.validate_round_authorization(state)


def test_pre_authorization_round_resumes_only_with_matching_start_event(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="resume-preview")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    assert store.validate_pre_authorization(state)["status"] == "preauthorization_safe_to_resume"
    store.authorize_round(state, [21])
    assert store.validate_round_authorization(state) == state.pending_round["authorization_id"]
    with pytest.raises(RuntimeError, match="cannot replace"):
        store.authorize_round(state, [22])


def test_checkpoint_journal_authorization_divergence_fails_closed(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="journal-divergence")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    store.authorize_round(state, [21])
    rows = [json.loads(line) for line in store.journal_path.read_text().splitlines()]
    rows = [
        row for row in rows
        if row["event"] not in {"round_authorization_prepared", "round_authorized"}
    ]
    store.journal_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(RuntimeError, match="missing its prepared journal event"):
        store.validate_round_authorization(state)


def test_interrupted_authorization_is_completed_from_matching_prepared_event(tmp_path, monkeypatch):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="auth-interrupted")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    original_append = store._append_event

    def interrupt_before_authorized(current, event, **fields):
        if event == "round_authorized":
            raise OSError("simulated interruption")
        return original_append(current, event, **fields)

    monkeypatch.setattr(store, "_append_event", interrupt_before_authorized)
    with pytest.raises(OSError, match="simulated interruption"):
        store.authorize_round(state, [31])
    assert state.pending_round["authorization_status"] == "prepared"
    monkeypatch.setattr(store, "_append_event", original_append)
    recovered_id = store.validate_round_authorization(state)
    assert recovered_id == state.pending_round["authorization_id"]
    assert state.pending_round["authorization_status"] == "authorized"
    events = [json.loads(line)["event"] for line in store.journal_path.read_text().splitlines()]
    assert events.count("round_authorization_prepared") == 1
    assert events.count("round_authorized") == 1


def test_orphan_prepared_event_blocks_cycle_without_pending_checkpoint(tmp_path, monkeypatch):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="orphan-prepared")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    original_append = store._append_event

    def interrupt(current, event, **fields):
        if event == "round_authorized":
            raise OSError("simulated crash after prepare")
        return original_append(current, event, **fields)

    monkeypatch.setattr(store, "_append_event", interrupt)
    with pytest.raises(OSError):
        store.authorize_round(state, [41])
    state.pending_round = None  # checkpoint/journal mismatch under audit
    store._write_checkpoint(state)

    with pytest.raises(RuntimeError, match="prepared cycle authorization"):
        store.validate_no_unresolved_authorizations(state)


def test_later_preview_cannot_pass_an_earlier_orphan_prepared_event(tmp_path, monkeypatch):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="prior-orphan-prepared")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    original_append = store._append_event

    def interrupt(current, event, **fields):
        if event == "round_authorized":
            raise OSError("simulated crash after prepare")
        return original_append(current, event, **fields)

    monkeypatch.setattr(store, "_append_event", interrupt)
    with pytest.raises(OSError):
        store.authorize_round(state, [43])
    monkeypatch.setattr(store, "_append_event", original_append)
    state.pending_round = None
    state.rounds = 1
    store._write_checkpoint(state)
    store.start_round(state, round_number=2, request={"batch_size": 1})

    with pytest.raises(RuntimeError, match="prepared cycle authorization"):
        store.validate_pre_authorization(state)


def test_round_intent_mismatch_in_authorized_event_fails_closed(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="auth-intent-mismatch")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    store.authorize_round(state, [42])
    events = [json.loads(line) for line in store.journal_path.read_text().splitlines()]
    for event in events:
        if event["event"] == "round_authorized":
            event["round_intent_id"] = "different-intent"
    store.journal_path.write_text("".join(json.dumps(event) + "\n" for event in events))

    with pytest.raises(RuntimeError, match="conflicts with checkpoint"):
        store.validate_round_authorization(state)


def test_pre_authorization_journal_with_authorized_event_is_blocked(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="preauth-divergence")
    intent_id = store.start_round(state, round_number=1, request={"batch_size": 1})
    store._append_event(
        state,
        "round_authorized",
        round_number=1,
        selected_ids=[44],
        authorization_id=intent_id,
        round_intent_id=intent_id,
    )
    with pytest.raises(RuntimeError, match="authorization absent from checkpoint"):
        store.validate_pre_authorization(state)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_start", "does not match cycle journal"),
        ("invalid_time", "timestamp is invalid"),
        ("divergent_request", "request diverges"),
    ],
)
def test_pre_authorization_checkpoint_requires_complete_matching_event(tmp_path, mutation, message):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id=f"invalid-{mutation}")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    rows = [json.loads(line) for line in store.journal_path.read_text().splitlines()]
    if mutation == "missing_start":
        rows = [row for row in rows if row["event"] != "round_started"]
        store.journal_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    elif mutation == "invalid_time":
        for row in rows:
            if row["event"] == "round_started":
                row["at"] = "not-a-timestamp"
        store.journal_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    else:
        state.pending_round["request"]["batch_size"] = 9
        store._write_checkpoint(state)
    with pytest.raises(RuntimeError, match=message):
        store.validate_pre_authorization(state)


def test_pre_authorization_blocks_if_earlier_authorized_round_is_unresolved(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 2}, cycle_id="earlier-round-open")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    store.authorize_round(state, [61])
    state.pending_round = None
    state.rounds = 1
    store.start_round(state, round_number=2, request={"batch_size": 1})
    with pytest.raises(RuntimeError, match="earlier authorized round remains unresolved"):
        store.validate_pre_authorization(state)


def test_pre_authorization_blocks_post_journal_or_registry_changes_after_start(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="post-start-effects")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    post_journal = tmp_path / "work" / "v2-journal" / "77.json"
    post_journal.parent.mkdir(parents=True, exist_ok=True)
    post_journal.write_text(json.dumps({"post_id": 77, "writer_started": True}))
    with pytest.raises(RuntimeError, match="post or media evidence changed"):
        store.validate_pre_authorization(state)


def test_pre_authorization_blocks_media_registry_effect_after_start(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="media-after-start")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    registry = tmp_path / "work" / "media_registry.sqlite"
    with sqlite3.connect(registry) as db:
        db.execute("CREATE TABLE unexpected_media_effect (id INTEGER)")
    with pytest.raises(RuntimeError, match="post or media evidence changed"):
        store.validate_pre_authorization(state)


def _create_wal_registry(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO evidence(value) VALUES ('stable')")
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _create_legacy_registry(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO evidence(value) VALUES ('stable')")
        db.commit()


def test_pre_authorization_ignores_empty_wal_and_shm_created_by_reader(tmp_path):
    registry = tmp_path / "work" / "media_registry.sqlite"
    _create_wal_registry(registry)
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="empty-wal-reader")
    store.start_round(state, round_number=1, request={"batch_size": 1})

    with sqlite3.connect(registry) as db:
        assert db.execute("SELECT value FROM evidence").fetchone()[0] == "stable"

    assert Path(f"{registry}-wal").exists()
    assert Path(f"{registry}-wal").stat().st_size == 0
    evidence = store.validate_pre_authorization(state)
    assert evidence["status"] == "preauthorization_safe_to_resume"
    assert evidence["registry_fingerprint"]["status"] == "ok"


@pytest.mark.parametrize("mutation", ["insert", "update", "delete", "schema"])
def test_pre_authorization_blocks_each_real_registry_mutation(tmp_path, mutation):
    registry = tmp_path / "work" / "media_registry.sqlite"
    _create_legacy_registry(registry)
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id=f"registry-{mutation}")
    store.start_round(state, round_number=1, request={"batch_size": 1})

    with sqlite3.connect(registry) as db:
        if mutation == "insert":
            db.execute("INSERT INTO evidence(value) VALUES ('new')")
        elif mutation == "update":
            db.execute("UPDATE evidence SET value='changed' WHERE id=1")
        elif mutation == "delete":
            db.execute("DELETE FROM evidence WHERE id=1")
        else:
            db.execute("ALTER TABLE evidence ADD COLUMN extra TEXT")
        db.commit()

    with pytest.raises(RuntimeError, match="post or media evidence changed"):
        store.validate_pre_authorization(state)


def test_pre_authorization_blocks_nonempty_wal_without_stable_evidence(tmp_path):
    registry = tmp_path / "work" / "media_registry.sqlite"
    _create_wal_registry(registry)
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="nonempty-wal")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    Path(f"{registry}-wal").write_bytes(b"unclassified wal frame")

    with pytest.raises(RuntimeError, match="post or media evidence changed"):
        store.validate_pre_authorization(state)


def test_pre_authorization_blocks_registry_race_during_audit(tmp_path, monkeypatch):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="registry-race")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    original = cycle_state_module._capture_registry_fingerprint
    monkeypatch.setattr(
        cycle_state_module,
        "_capture_registry_fingerprint",
        lambda _path: {
            "status": "inconclusive",
            "reason": "registry_changed_during_audit",
        },
    )
    with pytest.raises(RuntimeError, match="post or media evidence changed"):
        store.validate_pre_authorization(state)
    monkeypatch.setattr(cycle_state_module, "_capture_registry_fingerprint", original)


def test_pre_authorization_audit_is_idempotent_and_preserves_round_identity(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="idempotent-preauth")
    intent_id = store.start_round(state, round_number=1, request={"batch_size": 1})
    first = store.validate_pre_authorization(state)
    second = store.validate_pre_authorization(state)
    assert first == second
    assert first["cycle_id"] == "idempotent-preauth"
    assert first["round_intent_id"] == intent_id
    assert state.pending_round["authorization_id"] is None
    assert state.pending_round["preview_selected_ids"] is None


def test_pre_authorization_recovery_archives_checkpoint_journal_registry_and_preview(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="archive-preview")
    registry = tmp_path / "work" / "media_registry.sqlite"
    registry.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(registry) as db:
        db.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO evidence(value) VALUES ('preserved')")
    intent_id = store.start_round(state, round_number=1, request={"batch_size": 1})

    result = store.archive_pre_authorization(state)
    archive = tmp_path / result["path"]
    manifest = json.loads((archive / "manifest.json").read_text())
    assert result["status"] == "archived"
    assert manifest["round_intent_id"] == intent_id
    assert manifest["files"]["media_registry.sqlite"]["status"] == "archived"
    assert manifest["preview"]["status"] == "not_run_yet"
    assert stat.S_IMODE((archive / "state.json").stat().st_mode) == 0o600
    with sqlite3.connect(archive / "media_registry.sqlite") as db:
        assert db.execute("SELECT value FROM evidence").fetchone()[0] == "preserved"

    store.record_round_preview(state, [51], preview_sha256="a" * 64)
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest["preview"]["selected_ids"] == [51]
    assert manifest["preview"]["sha256"] == "a" * 64
    assert store.validate_pre_authorization(state)["status"] == "preauthorization_safe_to_resume"
