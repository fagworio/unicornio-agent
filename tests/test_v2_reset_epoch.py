import json
from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2.model import LifecycleState, Phase, WorkState
from unicornio_editor.pipeline_v2.reset_epoch import (
    ResetEpochError,
    _classify,
    _initial_state,
    _protected_ids,
    _quarantine,
    reset_epoch,
)


class FakeClient:
    def __init__(self, posts):
        self.posts = {int(p["id"]): p for p in posts}
        self.writes = []

    def list_pending(self, *, page=1, per_page=100, status="pending", include=None):
        return [p for p in self.posts.values() if p.get("status") == status]

    def get_post(self, post_id):
        return self.posts[int(post_id)]

    def update_post(self, post_id, payload):
        self.writes.append((post_id, payload))
        self.posts[int(post_id)]["meta"].update(payload.get("meta") or {})
        return self.posts[int(post_id)]

    def move_to_status(self, post_id, status):
        self.posts[int(post_id)]["status"] = status
        return self.posts[int(post_id)]


def post(pid, status="pending", state=None):
    return {"id": pid, "status": status, "featured_media": 0,
            "content": {"raw": f"content-{pid}"},
            "meta": {"_hermes_work_state": json.dumps(state.to_dict())} if state else {}}


def test_initial_state_uses_current_policy_and_relevance():
    state = _initial_state()
    assert state.state is LifecycleState.PENDING
    assert state.phase is Phase.RELEVANCE
    assert state.retry.attempts == 0
    assert state.retry.next_at is None
    assert state.relevance_approved is False
    assert state.media.to_dict()["inline"]["accepted"] == []


@pytest.mark.parametrize(
    "quarantine_posts",
    [[{"post_id": "unknown"}], [{"post_id": 0}], [{"post_id": 12}, {"post_id": "12"}], [None]],
)
def test_malformed_quarantine_entries_fail_closed(tmp_path, quarantine_posts):
    path = tmp_path / "config" / "v2-quarantine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "posts": quarantine_posts}), encoding="utf-8")

    with pytest.raises(ResetEpochError, match="quarantine"):
        _quarantine(tmp_path)


@pytest.mark.parametrize("relative_path", ["work/v2-journal/unknown.json", "work/locks/global.lock"])
def test_unmapped_marker_filenames_fail_closed(tmp_path, relative_path):
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ResetEpochError, match="uncategorized"):
        _protected_ids(tmp_path, registry_ids=set())




def test_preview_is_read_only_and_protects_115757(tmp_path):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(json.dumps({"version": 1, "posts": [], "release_history": []}))
    (tmp_path / "work" / "v2-cycle").mkdir(parents=True)
    (tmp_path / "work" / "v2-cycle" / "state.json").write_text(json.dumps({"schema_version": 1, "cycle_id": "old", "status": "running", "phase": "ADMISSION", "stop_reason": "recovery_uncertain", "pending_round": {"round_number": 1}, "round_reports": [{"unresolved_posts": [{"post_id": 115757}]}]}))
    from unicornio_editor.media.registry import connect
    connect(tmp_path).close()
    client = FakeClient([post(115757), post(12), post(99, "publish")])
    result = reset_epoch(client, object(), tmp_path, preview=True, report_dir=tmp_path / "report")
    assert result["status"] == "RESET_PREVIEW_ONLY"
    assert not client.writes
    classes = {row["post_id"]: row["classification"] for row in result["posts"]}
    assert classes[115757] == "protected_transaction"
    assert classes[12] == "reset"
    assert classes[99] == "preserve_published"


def test_non_editorial_status_is_excluded():
    row = _classify(Path("."), post(1, "draft"), {}, set())
    assert row["classification"] == "exclude_status"


def test_invalid_canonical_state_is_exception():
    value = post(1)
    value["meta"]["_hermes_work_state"] = "not-json"
    row = _classify(Path("."), value, {}, set())
    assert row["classification"] == "exception"


def test_published_v2_pending_is_not_reset():
    state = WorkState(state=LifecycleState.PUBLISHED, phase=Phase.PUBLISH)
    row = _classify(Path("."), post(1, "pending", state), {}, set())
    assert row["classification"] == "preserve_published"


def test_pending_authorized_round_protects_all_selected_posts(tmp_path):
    selected = [116223, 116190, 116231, 116233, 116192]
    cycle_dir = tmp_path / "work" / "v2-cycle"
    cycle_dir.mkdir(parents=True)
    (cycle_dir / "state.json").write_text(json.dumps({
        "schema_version": 1,
        "cycle_id": "f4cbf124822b4ff28556bada2c961379",
        "status": "running",
        "phase": "ADMISSION",
        "pending_round": {
            "round_number": 1,
            "authorization_id": "e22087e3d5be478fbde160ff52c6fb77",
            "authorization_status": "authorized",
            "preview_selected_ids": selected,
        },
    }), encoding="utf-8")

    protected = _protected_ids(tmp_path)

    assert set(selected) <= protected
    for post_id in selected:
        row = _classify(tmp_path, post(post_id), {}, protected)
        assert row["classification"] == "protected_transaction"


def test_pending_round_selection_without_auth_id_is_protected_until_classified(tmp_path):
    cycle_dir = tmp_path / "work" / "v2-cycle"
    cycle_dir.mkdir(parents=True)
    (cycle_dir / "state.json").write_text(json.dumps({
        "schema_version": 1,
        "cycle_id": "synthetic-cycle",
        "status": "running",
        "phase": "ADMISSION",
        "pending_round": {
            "round_number": 1,
            "authorization_id": None,
            "preview_selected_ids": [403, 404],
        },
    }), encoding="utf-8")

    protected = _protected_ids(tmp_path)

    assert {403, 404} <= protected
    assert all(
        _classify(tmp_path, post(post_id), {}, protected)["classification"] == "protected_transaction"
        for post_id in (403, 404)
    )


def test_incomplete_media_registry_operation_protects_pending_post(tmp_path):
    from unicornio_editor.media.registry import connect

    with connect(tmp_path) as db:
        db.execute(
            """INSERT INTO media_operations
            (post_id, run_id, cycle_id, cycle_round, authorization_id, phase,
             operation_status, started_at, write_started, readback_confirmed, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (116192, "synthetic-run", "synthetic-cycle", 1, "synthetic-auth",
             "relevance", "started", "2026-11-12T00:00:00+00:00", 0, 0, ""),
        )

    protected = _protected_ids(tmp_path)

    assert 116192 in protected
    assert _classify(tmp_path, post(116192), {}, protected)["classification"] == "protected_transaction"


def test_reset_protects_posts_with_lock_or_journal(tmp_path):
    locks = tmp_path / "work" / "locks"
    journals = tmp_path / "work" / "v2-journal"
    locks.mkdir(parents=True)
    journals.mkdir(parents=True)
    (locks / "400.lock").write_text("synthetic lock evidence", encoding="utf-8")
    (journals / "401.json").write_text("{}", encoding="utf-8")

    protected = _protected_ids(tmp_path)

    assert {400, 401} <= protected
    assert _classify(tmp_path, post(400), {}, protected)["classification"] == "protected_transaction"
    assert _classify(tmp_path, post(401), {}, protected)["classification"] == "protected_transaction"


def test_reset_protects_wordpress_state_divergence(tmp_path):
    canonical = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
    )
    value = post(402)
    value["meta"] = {
        "_hermes_work_state": json.dumps(canonical.to_dict()),
        "_hermes_state": "awaiting_human",
    }

    row = _classify(tmp_path, value, {}, set())

    assert row["classification"] == "protected_transaction"
    assert row["reason"] == "wordpress_state_divergence"


def test_reset_preview_fails_closed_when_media_registry_is_missing(tmp_path):
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "v2-quarantine.json").write_text(json.dumps({
        "version": 1,
        "posts": [],
        "release_history": [],
    }), encoding="utf-8")

    with pytest.raises(ResetEpochError, match="Media Registry"):
        reset_epoch(
            FakeClient([post(500)]),
            object(),
            tmp_path,
            preview=True,
            report_dir=tmp_path / "preview",
        )


def test_started_media_run_protects_post_without_operation_row(tmp_path):
    from unicornio_editor.media.registry import connect

    with connect(tmp_path) as db:
        db.execute(
            """INSERT INTO media_runs
            (run_id, post_id, planned_deficits, queries_attempted, acquisition_outcome,
             checkpoint_reference, completion_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("run-600", 600, "{}", 0, "started", "", "relevance", "2026-11-12T00:00:00+00:00"),
        )

    protected = _protected_ids(tmp_path)

    assert 600 in protected
    assert _classify(tmp_path, post(600), {}, protected)["classification"] == "protected_transaction"


def test_media_usage_without_readback_protects_post(tmp_path):
    from unicornio_editor.media.registry import connect

    with connect(tmp_path) as db:
        for post_id, status, applied_at in ((601, "accepted", None), (602, "applied", "2026-11-12T00:00:00+00:00")):
            cursor = db.execute(
                "INSERT INTO media_assets (canonical_url) VALUES (?)",
                (f"https://example.test/{post_id}.webp",),
            )
            db.execute(
                """INSERT INTO media_usage
                (post_id, asset_id, wordpress_media_id, role, status, applied_at, readback_confirmed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (post_id, cursor.lastrowid, post_id, "inline", status, applied_at, None),
            )

    protected = _protected_ids(tmp_path)

    assert {601, 602} <= protected
    assert all(
        _classify(tmp_path, post(post_id), {}, protected)["classification"] == "protected_transaction"
        for post_id in (601, 602)
    )


def test_readback_confirmed_media_registry_rows_are_not_protected(tmp_path):
    from unicornio_editor.media.registry import connect

    with connect(tmp_path) as db:
        db.execute(
            """INSERT INTO media_runs
            (run_id, post_id, planned_deficits, queries_attempted, acquisition_outcome,
             checkpoint_reference, completion_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("run-610", 610, "{}", 0, "retry", "", "media", "2026-11-12T00:00:00+00:00"),
        )
        cursor = db.execute(
            "INSERT INTO media_assets (canonical_url) VALUES (?)",
            ("https://example.test/610.webp",),
        )
        db.execute(
            """INSERT INTO media_usage
            (post_id, asset_id, wordpress_media_id, role, status, applied_at, readback_confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (610, cursor.lastrowid, 610, "inline", "applied", "2026-11-12T00:00:00+00:00", "2026-11-12T00:01:00+00:00"),
        )
        db.execute(
            """INSERT INTO media_operations
            (post_id, run_id, cycle_id, cycle_round, authorization_id, phase,
             operation_status, started_at, completed_at, write_started, readback_confirmed, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (610, "operation-610", "cycle-610", 1, "auth-610", "media", "completed",
             "2026-11-12T00:00:00+00:00", "2026-11-12T00:01:00+00:00", 1, 1, "applied"),
        )

    assert 610 not in _protected_ids(tmp_path)
