from __future__ import annotations

import json
import hashlib

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
        pending_round={
            "round_number": ROUND,
            "authorization_id": AUTH_ID,
            "preview_selected_ids": post_ids,
        },
    )
    CycleStateStore(tmp_path).save(state, event="round_authorized", selected_ids=post_ids)

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
