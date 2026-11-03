from __future__ import annotations

import json
import hashlib

import pytest

from unicornio_editor.media.registry import connect
from unicornio_editor.pipeline_v2.cycle_investigation import (
    _probe_lock,
    audit_pending_round_readonly,
    build_investigation_queue,
    release_isolated_post,
)
from unicornio_editor.pipeline_v2.cycle_state import CycleState, CycleStateStore
from unicornio_editor.pipeline_v2.lock import RunSessionLock
from unicornio_editor.pipeline_v2.model import LifecycleState, Phase, WorkState
from unicornio_editor.locking import LockManager
from unicornio_editor.cli import build_parser, main


POST_ID = 115975
CYCLE_ID = "investigation-cycle"
ROUND = 3
AUTH_ID = "investigation-authorization"
RUN_ID = "investigation-run"
ISOLATION_ID = "a" * 24


def _fixture(root, *, uncertain: bool = False, now: float = 1_800_000_000.0):
    (root / "work" / "v2-cycle" / "transitions").mkdir(parents=True)
    (root / "work" / "v2-journal").mkdir(parents=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.RELEVANCE,
    ).to_dict()
    journal = {
        "post_id": POST_ID,
        "run_id": RUN_ID,
        "cycle_id": CYCLE_ID,
        "cycle_round": ROUND,
        "authorization_id": AUTH_ID,
        "status": "committing" if uncertain else "committed",
        "writer_started": True,
        "readback": False if uncertain else True,
        "state": state,
    }
    (root / "work" / "v2-journal" / f"{POST_ID}.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )
    with connect(root) as db:
        db.execute(
            "INSERT INTO media_runs(run_id, post_id, created_at) VALUES (?, ?, ?)",
            (RUN_ID, POST_ID, "2026-11-03T00:00:00+00:00"),
        )
    (root / "config" / "v2-quarantine.json").write_text(
        json.dumps({
            "version": 1,
            "posts": [{
                "post_id": POST_ID,
                "reason_code": "uncertain_transaction",
                "reason": f"UNCERTAIN_ISOLATED {ISOLATION_ID}; no replay until resolved",
            }],
            "release_history": [],
        }),
        encoding="utf-8",
    )
    transition = {
        "version": 3,
        "phase": "completed",
        "isolation_id": ISOLATION_ID,
        "cycle_id": CYCLE_ID,
        "authorization_id": AUTH_ID,
        "pending_round": ROUND,
        "selected_ids": [POST_ID],
        "classes": {str(POST_ID): "UNCERTAIN_ISOLATED"},
        "created_at": now - 7200,
    }
    (root / "work" / "v2-cycle" / "transitions" / f"{ISOLATION_ID}.json").write_text(
        json.dumps(transition), encoding="utf-8"
    )

    class Client:
        writes = 0

        def get_post(self, post_id):
            assert post_id == POST_ID
            return {
                "id": POST_ID,
                "status": "pending",
                "modified": "2026-11-03T00:00:00",
                "featured_media": 0,
                "meta": {"_hermes_work_state": state},
                "content": {"raw": "body"},
                "title": {"raw": "title"},
            }

    return Client()


def test_investigation_queue_reports_readonly_current_evidence_age_and_alerts(tmp_path):
    now = 1_800_000_000.0
    client = _fixture(tmp_path, now=now)
    before = (tmp_path / "config" / "v2-quarantine.json").read_bytes()

    report = build_investigation_queue(
        tmp_path,
        client,
        now=now,
        count_alert_threshold=1,
        age_alert_seconds=3600,
    )

    assert report["status"] == "ok"
    assert report["read_only"] is True
    assert report["metrics"]["isolated_count"] == 1
    assert report["metrics"]["oldest_age_seconds"] == 7200
    assert {"code": "isolated_queue_high", "count": 1} in report["alerts"]
    assert {"code": "oldest_isolation_overdue", "age_seconds": 7200} in report["alerts"]
    row = report["posts"][0]
    assert row["post_id"] == POST_ID
    assert row["classification"] == "COMMITTED_NO_REPLAY"
    assert row["evidence_status"] == "current_evidence_available"
    assert row["next_action"] == "review_read_only_evidence_then_authorize_release"
    assert row["evidence"]["journal"]["sha256"]
    assert row["evidence"]["registry"]["schema_complete"] is True
    assert (tmp_path / "config" / "v2-quarantine.json").read_bytes() == before


def test_release_requires_fresh_classifier_proof_and_records_authorized_decision(tmp_path):
    client = _fixture(tmp_path)
    policy_path = tmp_path / "config" / "v2-quarantine.json"
    before = policy_path.read_bytes()
    kwargs = {
        "post_id": POST_ID,
        "authorization_id": "release-auth-115975",
        "authorized_by": "reviewer",
        "reason": "Readback and Registry evidence independently confirmed the committed transaction.",
    }

    preview = release_isolated_post(tmp_path, client, **kwargs)
    assert preview["status"] == "preview"
    assert preview["eligible_for_apply"] is True
    assert preview["classification"] == "COMMITTED_NO_REPLAY"
    assert preview["wordpress_writes"] == 0
    assert policy_path.read_bytes() == before

    released = release_isolated_post(
        tmp_path,
        client,
        **kwargs,
        preview=preview,
        apply=True,
    )

    assert released["status"] == "released"
    assert released["wordpress_writes"] == 0
    assert released["registry_writes"] == 0
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    assert policy["posts"] == []
    assert policy["release_history"][0]["classification"] == "COMMITTED_NO_REPLAY"
    assert policy["release_history"][0]["authorized_by"] == "reviewer"
    assert policy["release_history"][0]["evidence_sha256"]
    assert build_investigation_queue(tmp_path, client)["metrics"]["isolated_count"] == 0


def test_uncertain_current_journal_cannot_be_released(tmp_path):
    client = _fixture(tmp_path, uncertain=True)

    result = release_isolated_post(
        tmp_path,
        client,
        post_id=POST_ID,
        authorization_id="release-auth-must-not-pass",
        authorized_by="reviewer",
        reason="Attempted release without resolved transaction evidence.",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "current_evidence_does_not_prove_safe_release"
    assert result["classification"] == "UNCERTAIN_ISOLATED"
    policy = json.loads((tmp_path / "config" / "v2-quarantine.json").read_text())
    assert [row["post_id"] for row in policy["posts"]] == [POST_ID]


def test_investigation_queue_ignores_selected_but_not_isolated_posts(tmp_path):
    _fixture(tmp_path)
    transition = {
        "phase": "completed",
        "isolation_id": ISOLATION_ID,
        "cycle_id": CYCLE_ID,
        "authorization_id": AUTH_ID,
        "pending_round": ROUND,
        "selected_ids": [POST_ID],
        "classes": {str(POST_ID): "COMMITTED_NO_REPLAY"},
    }
    (tmp_path / "work" / "v2-cycle" / "transitions" / f"{ISOLATION_ID}.json").write_text(
        json.dumps(transition), encoding="utf-8"
    )
    report = build_investigation_queue(tmp_path)
    assert report["posts"][0]["evidence_status"] == "unavailable"
    assert report["posts"][0]["classification"] == "UNCERTAIN_ISOLATED"
    assert report["posts"][0]["next_action"] == "locate_original_transition_and_evidence_before_any_release"

    result = release_isolated_post(
        tmp_path,
        object(),
        post_id=POST_ID,
        authorization_id="release-auth-not-isolated",
        authorized_by="reviewer",
        reason="This row was selected but not isolated.",
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "original_recovery_identity_unavailable"


def test_release_refuses_transition_that_has_not_completed(tmp_path):
    client = _fixture(tmp_path)
    transition_path = tmp_path / "work" / "v2-cycle" / "transitions" / f"{ISOLATION_ID}.json"
    transition = json.loads(transition_path.read_text(encoding="utf-8"))
    transition["phase"] = "checkpoint_applied"
    transition_path.write_text(json.dumps(transition), encoding="utf-8")

    result = release_isolated_post(
        tmp_path,
        client,
        post_id=POST_ID,
        authorization_id="release-auth-incomplete-transition",
        authorized_by="reviewer",
        reason="Transition must be complete first.",
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "recovery_transition_incomplete"


def test_lock_probe_uses_read_only_shared_lock_and_fails_closed(tmp_path):
    path = tmp_path / "v2-run.lock"
    assert _probe_lock(path) == "missing"

    lock = RunSessionLock(path)
    assert lock.acquire() is True
    try:
        assert _probe_lock(path) == "busy"
    finally:
        lock.release()

    # The process-wide RunSessionLock's kernel lock is authoritative; after
    # release, its empty marker must not create an artificial cooldown.
    assert _probe_lock(path, empty_recent_is_ambiguous=False) == "unlocked"

    post_lock = LockManager(tmp_path / "locks").acquire(POST_ID)
    try:
        assert _probe_lock(post_lock.path) == "busy"
    finally:
        post_lock.release()
    assert _probe_lock(post_lock.path) == "unlocked"


def test_five_post_lock_contexts_leave_released_records_not_ambiguous_owners(tmp_path):
    lock_root = tmp_path / "work" / "locks"
    manager = LockManager(lock_root, ttl=60)
    for post_id in range(115970, 115975):
        with manager.acquire(post_id):
            pass
        lock_path = lock_root / f"{post_id}.lock"
        record = json.loads(lock_path.read_text(encoding="utf-8"))
        assert record["released"] is True
        assert _probe_lock(lock_path) == "unlocked"


def test_pending_round_audit_cli_is_exposed_as_read_only_preflight(tmp_path, monkeypatch, capsys):
    parsed = build_parser().parse_args(["v2-audit-pending-round", "--root", str(tmp_path)])
    assert parsed.command == "v2-audit-pending-round"

    monkeypatch.setattr("unicornio_editor.cli._carregar_env_do_projeto", lambda: None)
    monkeypatch.setattr("unicornio_editor.cli.load_config", lambda: object())
    monkeypatch.setattr("unicornio_editor.cli.WordPressClient", lambda _config: object())
    assert main(["v2-audit-pending-round", "--root", str(tmp_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "no_pending_round"
    assert output["read_only"] is True
    assert output["wordpress_writes"] == 0
    assert output["replay"] is False


def test_pending_round_audit_classifies_pre_authorization_interruption_read_only(tmp_path):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="audit-preauthorization")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    before = {
        "state": hashlib.sha256(store.state_path.read_bytes()).hexdigest(),
        "journal": hashlib.sha256(store.journal_path.read_bytes()).hexdigest(),
    }
    report = audit_pending_round_readonly(tmp_path, object())
    after = {
        "state": hashlib.sha256(store.state_path.read_bytes()).hexdigest(),
        "journal": hashlib.sha256(store.journal_path.read_bytes()).hexdigest(),
    }
    assert report["status"] == "preauthorization_interrupted"
    assert report["classification"] == "SAFE_TO_RESUME_PREVIEW"
    assert report["authorization_id"] is None
    assert report["selected_ids"] is None
    assert report["wordpress_writes"] == report["wordpress_readbacks"] == 0
    assert report["evidence"]["writer_possible_by_protocol"] is False
    assert after == before


def test_pending_round_audit_reads_legacy_multiline_event_and_reports_format(tmp_path):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="legacy-multiline-audit")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    events = store._events()
    pretty = json.dumps(
        {**events[0], "legacy_fixture": "compat"}, ensure_ascii=False, indent=2
    )
    rest = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in events[1:])
    store.journal_path.write_text(pretty + "\n" + rest, encoding="utf-8")
    before = store.journal_path.read_bytes()

    report = audit_pending_round_readonly(tmp_path, object())

    assert report["status"] == "preauthorization_interrupted"
    assert report["journal_format"] == "journal_format_legacy_valid"
    assert store.journal_path.read_bytes() == before


def test_pending_round_audit_reports_corrupt_journal_before_any_wordpress_read(tmp_path):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="corrupt-audit")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    store.journal_path.write_bytes(store.journal_path.read_bytes() + b"{truncated")
    before = store.journal_path.read_bytes()

    class NoReadback:
        def get_post(self, _post_id):
            raise AssertionError("corrupt cycle journal must block before WordPress readback")

    report = audit_pending_round_readonly(tmp_path, NoReadback())
    assert report["status"] == "blocked"
    assert report["reason"] == "journal_corrupt"
    assert report["journal_error"] == "invalid_or_truncated_json_event"
    assert report["wordpress_writes"] == report["registry_writes"] == 0
    assert report["replay"] is False
    assert store.journal_path.read_bytes() == before


def test_pending_round_audit_blocks_journaled_auth_absent_from_checkpoint(tmp_path):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="audit-divergent")
    intent_id = store.start_round(state, round_number=1, request={"batch_size": 1})
    store._append_event(
        state,
        "round_authorized",
        round_number=1,
        selected_ids=[49],
        authorization_id=intent_id,
        round_intent_id=intent_id,
    )
    report = audit_pending_round_readonly(tmp_path, object())
    assert report["status"] == "blocked"
    assert report["reason"] == "preauthorization_checkpoint_journal_mismatch"
    assert report["wordpress_writes"] == 0


def test_pending_round_audit_blocks_authorization_journal_mismatch_before_readback(tmp_path):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="authorized-audit-divergence")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    store.authorize_round(state, [59])
    rows = [json.loads(line) for line in store.journal_path.read_text().splitlines()]
    rows = [row for row in rows if row["event"] != "round_authorized"]
    store.journal_path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    class NoReadback:
        reads = 0

        def get_post(self, _post_id):
            self.reads += 1
            raise AssertionError("divergent authorization must block before WordPress readback")

    client = NoReadback()
    report = audit_pending_round_readonly(tmp_path, client)
    assert report["status"] == "blocked"
    assert report["reason"] == "round_authorization_checkpoint_journal_mismatch"
    assert client.reads == 0


def _prepared_authorization_fixture(tmp_path, monkeypatch, *, post_id=115980):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="prepared-audit-cycle")
    store.start_round(state, round_number=1, request={"batch_size": 1})
    original_append = store._append_event

    def interrupt_before_authorized(state_arg, event, **payload):
        if event == "round_authorized":
            raise OSError("simulated interruption after durable preparation")
        return original_append(state_arg, event, **payload)

    monkeypatch.setattr(store, "_append_event", interrupt_before_authorized)
    with pytest.raises(OSError, match="simulated interruption"):
        store.authorize_round(state, [post_id])
    # Schema exists and is complete, but a zero-row Registry is not evidence of
    # a write; the audit still needs to establish this against the prepared time.
    with connect(tmp_path):
        pass
    return store


def test_prepared_authorization_requires_two_pass_no_write_proof(tmp_path, monkeypatch):
    store = _prepared_authorization_fixture(tmp_path, monkeypatch)

    class PendingReadback:
        writes = 0

        def __init__(self):
            self.reads = 0

        def get_post(self, post_id):
            self.reads += 1
            return {"id": post_id, "status": "pending", "meta": {}}

    client = PendingReadback()
    checkpoint_before = store.state_path.read_bytes()
    journal_before = store.journal_path.read_bytes()

    report = audit_pending_round_readonly(tmp_path, client)

    assert report["status"] == "authorization_prepared_recoverable"
    assert report["reason"] is None
    assert report["evidence"]["all_posts_independently_prove_no_write"] is True
    assert report["evidence"]["writer_started"] is False
    assert report["posts"][0]["classification"] == "NOT_EXECUTED"
    assert report["posts"][0]["no_write_proven"] is True
    assert report["posts"][0]["registry_complete"] is True
    assert report["wordpress_readbacks"] == client.reads == 4
    assert report["wordpress_writes"] == report["registry_writes"] == 0
    assert report["automatic_recovery"] is False
    assert report["replay"] is False
    assert store.state_path.read_bytes() == checkpoint_before
    assert store.journal_path.read_bytes() == journal_before


def test_prepared_authorization_with_post_writer_evidence_remains_blocked(tmp_path, monkeypatch):
    post_id = 115981
    store = _prepared_authorization_fixture(tmp_path, monkeypatch, post_id=post_id)
    pending = store.load().pending_round
    (tmp_path / "work" / "v2-journal").mkdir(parents=True)
    (tmp_path / "work" / "v2-journal" / f"{post_id}.json").write_text(
        json.dumps({
            "post_id": post_id,
            "run_id": "possibly-started-run",
            "cycle_id": "prepared-audit-cycle",
            "cycle_round": 1,
            "authorization_id": pending["authorization_id"],
            "status": "running",
            "writer_started": True,
            "readback": False,
            "state": {"state": "pending"},
        }),
        encoding="utf-8",
    )

    class PendingReadback:
        def __init__(self):
            self.reads = 0

        def get_post(self, requested_id):
            self.reads += 1
            return {"id": requested_id, "status": "pending", "meta": {}}

    client = PendingReadback()
    checkpoint_before = store.state_path.read_bytes()
    journal_before = store.journal_path.read_bytes()
    post_journal_before = (tmp_path / "work" / "v2-journal" / f"{post_id}.json").read_bytes()

    report = audit_pending_round_readonly(tmp_path, client)

    assert report["status"] == "blocked"
    assert report["reason"] == "prepared_round_effects_or_evidence_uncertain"
    assert report["evidence"]["all_posts_independently_prove_no_write"] is False
    assert report["evidence"]["writer_started"] is None
    assert report["posts"][0]["no_write_proven"] is False
    assert report["wordpress_readbacks"] == client.reads == 2
    assert report["wordpress_writes"] == report["registry_writes"] == 0
    assert store.state_path.read_bytes() == checkpoint_before
    assert store.journal_path.read_bytes() == journal_before
    assert (tmp_path / "work" / "v2-journal" / f"{post_id}.json").read_bytes() == post_journal_before


def test_prepared_authorization_with_registry_effect_remains_blocked(tmp_path, monkeypatch):
    post_id = 115982
    store = _prepared_authorization_fixture(tmp_path, monkeypatch, post_id=post_id)
    pending = store.load().pending_round
    with connect(tmp_path) as db:
        db.execute(
            """INSERT INTO media_operations(
                post_id, run_id, cycle_id, cycle_round, authorization_id,
                phase, operation_status, started_at, write_started
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                post_id,
                "registry-effect-run",
                "prepared-audit-cycle",
                1,
                pending["authorization_id"],
                "writer",
                "started",
                "2026-11-03T12:00:01+00:00",
                1,
            ),
        )

    class PendingReadback:
        def get_post(self, requested_id):
            return {"id": requested_id, "status": "pending", "meta": {}}

    report = audit_pending_round_readonly(tmp_path, PendingReadback())

    assert report["status"] == "blocked"
    assert report["reason"] == "prepared_round_effects_or_evidence_uncertain"
    assert report["posts"][0]["classification"] == "UNCERTAIN_ISOLATED"
    assert report["posts"][0]["no_write_proven"] is False
    assert report["evidence"]["writer_started"] is None
    assert report["wordpress_writes"] == report["registry_writes"] == 0


def test_pending_round_audit_classifies_five_posts_read_only_with_real_classifier(tmp_path):
    post_ids = [115975, 115976, 115977, 115978, 115979]
    run_ids = {post_id: f"run-{post_id}" for post_id in post_ids}
    ready_state = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
    ).to_dict()
    (tmp_path / "work" / "v2-journal").mkdir(parents=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )
    with connect(tmp_path) as db:
        for post_id, run_id in run_ids.items():
            (tmp_path / "work" / "v2-journal" / f"{post_id}.json").write_text(
                json.dumps({
                    "post_id": post_id,
                    "run_id": run_id,
                    "cycle_id": CYCLE_ID,
                    "cycle_round": ROUND,
                    "authorization_id": AUTH_ID,
                    "status": "committed",
                    "writer_started": True,
                    "readback": True,
                    "state": ready_state,
                }),
                encoding="utf-8",
            )
            db.execute(
                "INSERT INTO media_runs(run_id, post_id, created_at) VALUES (?, ?, ?)",
                (run_id, post_id, "2026-11-03T12:00:00+00:00"),
            )
            db.execute(
                """INSERT INTO media_operations(
                    post_id, run_id, cycle_id, cycle_round, authorization_id,
                    phase, operation_status, started_at, completed_at,
                    write_started, readback_confirmed, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    post_id, run_id, CYCLE_ID, ROUND, AUTH_ID, "writer", "committed",
                    "2026-11-03T12:00:01+00:00", "2026-11-03T12:00:02+00:00", 1, 1, "ready",
                ),
            )

    state = CycleState(
        cycle_id=CYCLE_ID,
        phase="DRAINING",
        seen_post_ids=post_ids,
        attempts_by_post={post_id: 3 for post_id in post_ids},
        rounds=ROUND - 1,
    )
    store = CycleStateStore(tmp_path)
    store.save(state, event="cycle_started")
    store.start_round(
        state,
        round_number=ROUND,
        request={"batch_size": len(post_ids)},
        authorization_id=AUTH_ID,
    )
    store.authorize_round(state, post_ids)

    class ReadbackClient:
        writes = 0

        def __init__(self):
            self.reads = 0

        def get_post(self, post_id):
            self.reads += 1
            return {
                "id": post_id,
                "status": "pending",
                "modified": "2026-11-03T12:00:02",
                "featured_media": 9001,
                "meta": {"_hermes_work_state": ready_state},
                "content": {"raw": "body"},
                "title": {"raw": "title"},
            }

    client = ReadbackClient()
    evidence_paths = [
        tmp_path / "work" / "v2-cycle" / "state.json",
        tmp_path / "work" / "v2-cycle" / "journal.jsonl",
        tmp_path / "config" / "v2-quarantine.json",
        tmp_path / "work" / "media_registry.sqlite",
        *[tmp_path / "work" / "v2-journal" / f"{post_id}.json" for post_id in post_ids],
    ]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in evidence_paths}

    report = audit_pending_round_readonly(tmp_path, client)

    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in evidence_paths}
    assert report["status"] == "ok"
    assert report["read_only"] is True
    assert report["selected_ids"] == post_ids
    assert report["classification_counts"] == {"COMMITTED_NO_REPLAY": 5}
    assert report["posts"][0]["post_id"] == 115975
    assert report["posts"][0]["evidence"]["registry"]["snapshot_sha256"]
    assert report["automatic_recovery"] is False
    assert report["operator_decision_required"] is True
    assert report["wordpress_writes"] == 0
    assert report["replay"] is False
    assert client.reads == len(post_ids) * 2
    assert before == after
