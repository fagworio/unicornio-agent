import json
from types import SimpleNamespace

import pytest

from unicornio_editor.pipeline_v2.model import LifecycleState, Phase, WorkState
from unicornio_editor.pipeline_v2.runtime import (
    _write_v2_journal_checkpoint,
    _write_v2_journal_payload,
    run_v2,
)


class Client:
    def __init__(self, posts):
        self.posts = {post["id"]: post for post in posts}
        self.updates = []

    def list_pending(self, *, page, per_page, status):
        return list(self.posts.values()) if page == 1 else []

    def get_post(self, post_id):
        return self.posts[post_id]

    def update_post(self, post_id, payload):
        self.updates.append((post_id, payload))


def post(post_id, date, state):
    return {
        "id": post_id,
        "status": "pending",
        "date_gmt": date,
        "title": {"raw": f"Post {post_id}"},
        "content": {"raw": "<p>Texto editorial em português.</p>"},
        "meta": {"_hermes_work_state": json.dumps(state.to_dict())},
    }


def config(cohort_path, root):
    quarantine_file = root / "config" / "v2-quarantine.json"
    quarantine_file.parent.mkdir(parents=True, exist_ok=True)
    quarantine_file.write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )
    return SimpleNamespace(
        v2_historical_cohort_file=cohort_path,
        v2_admission_after="2026-10-09T00:00:00Z",
        v2_admission_allowlist=(),
    )




def test_preview_requeues_human_required_when_human_moves_wp_status_to_pending(tmp_path):
    state = WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA)
    reopened = post(3, "2026-10-09T01:00:00Z", state)
    reopened["meta"]["_hermes_work_state"] = json.dumps(state.to_dict())
    reopened["meta"]["_hermes_human_reopened_at"] = "2030-01-01T00:00:00+00:00"
    reopened["meta"]["_hermes_human_reopened_from"] = "awaiting_human"
    client = Client([reopened])

    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"version": 1, "posts": []}), encoding="utf-8")
    result = run_v2(client, config(cohort, tmp_path), tmp_path, post_id=3, preview=True)

    assert result["selected"] == 1
    assert result["queue"]["requested_pending"] is True
    assert result["queue"]["requested_eligible"] is True
    assert result["queue"]["selected_ids"] == [3]


def test_preview_requires_reopen_source_marker(tmp_path):
    state = WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA)
    reopened = post(5, "2026-10-09T01:00:00Z", state)
    reopened["meta"]["_hermes_human_reopened_at"] = "2030-01-01T00:00:00+00:00"
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"version": 1, "posts": []}), encoding="utf-8")

    result = run_v2(Client([reopened]), config(cohort, tmp_path), tmp_path, post_id=5, preview=True)

    assert result["selected"] == 0




def test_preview_does_not_requeue_human_required_without_manual_reopen_marker(tmp_path):
    state = WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA)
    reopened = post(4, "2026-10-09T01:00:00Z", state)
    client = Client([reopened])
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"version": 1, "posts": []}), encoding="utf-8")

    result = run_v2(client, config(cohort, tmp_path), tmp_path, post_id=4, preview=True)

    assert result["selected"] == 0
    assert result["queue"]["requested_eligible"] is False


def test_preview_post_id_uses_admission_and_never_falls_back(tmp_path):
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({
        "version": 1,
        "posts": [{
            "post_id": 1,
            "original_datetime": "2026-10-01T00:00:00Z",
            "classification": "historical",
        }],
    }), encoding="utf-8")
    state = WorkState(state=LifecycleState.PENDING, phase=Phase.RELEVANCE)
    client = Client([
        post(1, "2026-10-01T00:00:00Z", state),
        post(2, "2026-10-09T01:00:00Z", state),
    ])

    excluded = run_v2(client, config(cohort, tmp_path), tmp_path, post_id=1, preview=True)
    assert excluded["preview"] is True
    assert excluded["selected"] == 0
    assert excluded["queue"]["request_reason"] == "not_admitted"
    assert client.updates == []

    selected = run_v2(client, config(cohort, tmp_path), tmp_path, post_id=2, preview=True)
    assert selected["selected"] == 1
    assert selected["queue"]["selected_ids"] == [2]
    assert selected["details"][0]["post_id"] == 2
    assert client.updates == []



def test_write_rejects_selection_drift_before_stages(tmp_path):
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"version": 1, "posts": []}), encoding="utf-8")
    client = Client([post(2, "2026-10-09T01:00:00Z", WorkState(state=LifecycleState.PENDING))])
    result = run_v2(
        client,
        config(cohort, tmp_path),
        tmp_path,
        post_id=2,
        expected_selected_ids=[999],
    )
    assert result["selection_mismatch"] is True
    assert result["expected_selected_ids"] == [999]
    assert result["actual_selected_ids"] == [2]
    assert client.updates == []


def test_write_pins_preview_ids_when_queue_order_changes(tmp_path):
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"version": 1, "posts": []}), encoding="utf-8")
    client = Client([
        # Keep post 1 older so the scheduler's time-based aged-NEW slot cannot
        # accidentally select the authorized post 2 during this test.
        post(1, "2026-10-09T01:00:00Z", WorkState(state=LifecycleState.PENDING)),
        post(2, "2026-10-09T03:00:00Z", WorkState(state=LifecycleState.PENDING)),
    ])
    result = run_v2(
        client,
        config(cohort, tmp_path),
        tmp_path,
        limit=1,
        expected_selected_ids=[2],
    )
    assert result.get("selection_mismatch") is not True
    assert result["queue"]["selection_revalidated"] is True
    assert result["queue"]["selection_drift"] is True
    assert result["queue"]["selected_ids"] == [2]


def test_write_preflight_preserves_incomplete_journal_when_selection_is_valid(tmp_path):
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"version": 1, "posts": []}), encoding="utf-8")
    client = Client([
        post(2, "2026-10-09T03:00:00Z", WorkState(state=LifecycleState.PENDING))
    ])
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    journal_path = journal_dir / "2.json"
    journal = {
        "status": "running",
        "post_id": 2,
        "run_id": "previous-run",
        "cycle_id": "previous-cycle",
        "cycle_round": 4,
    }
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    before = journal_path.read_text(encoding="utf-8")

    result = run_v2(
        client,
        config(cohort, tmp_path),
        tmp_path,
        limit=1,
        expected_selected_ids=[2],
        cycle_id="current-cycle",
        cycle_round=4,
        authorization_id="current-authorization",
    )

    assert result["journal_conflict"] is True
    assert result["journal_conflicts"] == [{
        "post_id": 2,
        "reason": "incomplete_journal",
        "status": "running",
        "run_id": "previous-run",
        "cycle_id": "previous-cycle",
        "cycle_round": 4,
        "authorization_id": None,
    }]
    assert client.updates == []
    assert journal_path.read_text(encoding="utf-8") == before


def test_journal_checkpoint_guard_preserves_running_journal(tmp_path):
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    journal_path = journal_dir / "2.json"
    journal_path.write_text(json.dumps({
        "status": "running",
        "post_id": 2,
        "run_id": "previous-run",
    }), encoding="utf-8")
    before = journal_path.read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match="cannot overwrite incomplete"):
        _write_v2_journal_checkpoint(
            tmp_path,
            2,
            "running",
            WorkState(state=LifecycleState.PENDING),
            run_id="new-run",
        )

    assert journal_path.read_text(encoding="utf-8") == before


def test_journal_payload_is_atomic_and_preserves_other_run(tmp_path):
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    journal_path = journal_dir / "2.json"
    previous = {
        "status": "running",
        "post_id": 2,
        "run_id": "previous-run",
        "state": {"state": "pending"},
    }
    journal_path.write_text(json.dumps(previous), encoding="utf-8")
    before = journal_path.read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match="cannot overwrite incomplete"):
        _write_v2_journal_payload(
            journal_path,
            {
                "status": "committing",
                "post_id": 2,
                "run_id": "new-run",
                "state": {"state": "ready"},
            },
        )

    assert journal_path.read_text(encoding="utf-8") == before
    assert not journal_path.with_suffix(".json.tmp").exists()

    payload = {
        "status": "committing",
        "post_id": 2,
        "run_id": "previous-run",
        "state": {"state": "ready"},
        "media_records": [{"media_id": 7}],
    }
    _write_v2_journal_payload(journal_path, payload)
    assert json.loads(journal_path.read_text(encoding="utf-8")) == payload
    assert not journal_path.with_suffix(".json.tmp").exists()


def test_quarantine_blocks_reentry_and_manual_post_id_but_not_other_eligible_posts(tmp_path):
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"version": 1, "posts": []}), encoding="utf-8")
    settings = config(cohort, tmp_path)
    settings.v2_admission_allowlist = (3,)
    quarantine = tmp_path / "config" / "v2-quarantine.json"
    quarantine.write_text(json.dumps({
        "version": 1,
        "posts": [{
            "post_id": 3,
            "reason_code": "human_reentry_unverified",
            "reason": "Reentrada sem autoria humana verificável.",
        }],
        "release_history": [],
    }), encoding="utf-8")
    reopened_state = WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA)
    reopened = post(3, "2026-10-01T01:00:00Z", reopened_state)
    reopened["meta"].update({
        "_hermes_human_reopened_at": "2030-01-01T00:00:00+00:00",
        "_hermes_human_reopened_from": "awaiting_human",
    })
    normal = post(4, "2026-10-09T01:00:00Z", WorkState(state=LifecycleState.PENDING))
    client = Client([reopened, normal])

    blocked = run_v2(client, settings, tmp_path, post_id=3, preview=True)
    assert blocked["selected"] == 0
    assert blocked["queue"]["requested_pending"] is True
    assert blocked["queue"]["requested_admitted"] is True
    assert blocked["queue"]["requested_eligible"] is False
    assert blocked["queue"]["request_reason"] == "quarantined"
    assert blocked["queue"]["requested_quarantine"]["reason_code"] == "human_reentry_unverified"
    assert blocked["queue"]["quarantined_posts"] == [{
        "post_id": 3,
        "reason_code": "human_reentry_unverified",
        "reason": "Reentrada sem autoria humana verificável.",
    }]

    general = run_v2(client, settings, tmp_path, preview=True)
    assert general["queue"]["selected_ids"] == [4]
    assert general["queue"]["quarantined_ids"] == [3]
    assert client.updates == []


def test_missing_or_invalid_quarantine_policy_fails_closed(tmp_path):
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"version": 1, "posts": []}), encoding="utf-8")
    state = WorkState(state=LifecycleState.PENDING)
    candidate = post(8, "2026-10-09T01:00:00Z", state)
    client = Client([candidate])
    settings = config(cohort, tmp_path)
    quarantine = tmp_path / "config" / "v2-quarantine.json"
    quarantine.unlink()

    missing = run_v2(client, settings, tmp_path, post_id=8, preview=True)
    assert missing["selected"] == 0
    assert missing["queue"]["quarantine_fail_closed"] is True
    assert missing["queue"]["request_reason"] == "quarantine_policy_invalid"

    quarantine.write_text(json.dumps({"version": 1, "posts": "bad", "release_history": []}))
    invalid = run_v2(client, settings, tmp_path, post_id=8, preview=True)
    assert invalid["selected"] == 0
    assert "must be lists" in invalid["queue"]["quarantine_policy_error"]
    assert invalid["queue"]["pre_quarantine_eligible"] == 1

    quarantine.write_text(json.dumps({
        "version": 1, "posts": [],
        "release_history": [{"post_id": 3, "authorized_by": "editor", "reason": "reviewed"}],
    }), encoding="utf-8")
    unaudited_release = run_v2(client, settings, tmp_path, post_id=8, preview=True)
    assert unaudited_release["selected"] == 0
    assert "authorization_id" in unaudited_release["queue"]["quarantine_policy_error"]
    assert client.updates == []
