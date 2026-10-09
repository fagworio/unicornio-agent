import json
from datetime import datetime, timezone

from unicornio_editor.pipeline_v2.audit import audit_human_required_inventory, historical_cohort_report
from unicornio_editor.pipeline_v2.model import (
    BlockerCode,
    InlineMedia,
    LifecycleState,
    MediaProgress,
    Phase,
    RetryInfo,
    WorkState,
)


def _state(*, blocker=BlockerCode.INLINE_MISSING, media=None, phase=Phase.MEDIA):
    return WorkState(
        state=LifecycleState.HUMAN_REQUIRED,
        phase=phase,
        blocker=blocker,
        retry=RetryInfo(
            attempts=8,
            no_progress=2,
            policy_version=3,
            phase_attempts=1,
        ),
        media=media or MediaProgress(
            required=2,
            inline=(InlineMedia(17, "https://cdn.test/a.webp", 0),),
        ),
    )


def test_audit_uses_existing_allowlisted_repair_signature_without_writing(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps({
            "posts": [{
                "post_id": 114835,
                "state": _state().to_dict(),
            }],
        }),
        encoding="utf-8",
    )

    report = audit_human_required_inventory(inventory)

    assert report["read_only"] is True
    assert report["counts"] == {"technical_known": 1}
    post = report["posts"][0]
    assert post["repair"] == "v2-repair-historical-media"
    assert post["eligible"] is True
    assert post["media"]["inline_media_ids"] == [17]


def test_audit_refuses_legitimate_terminal_state_and_unknown_state(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps({
            "posts": [
                {"post_id": 1, "state": _state(blocker=BlockerCode.TEXT_QUALITY).to_dict()},
                {"post_id": 2, "state": {"state": "human_required"}},
            ],
        }),
        encoding="utf-8",
    )

    report = audit_human_required_inventory(inventory)

    assert report["counts"] == {
        "legitimate_block": 1,
        "evidence_insufficient": 1,
    }
    assert report["posts"][0]["eligible"] is False
    assert report["posts"][1]["next_action"] == "capture_complete_v2_state"


def test_audit_does_not_treat_non_terminal_state_as_repairable(tmp_path):
    inventory = tmp_path / "inventory.json"
    state = _state()
    state = WorkState(
        state=LifecycleState.PENDING,
        phase=state.phase,
        blocker=state.blocker,
        retry=state.retry,
        media=state.media,
    )
    inventory.write_text(
        json.dumps([{"post_id": 114835, "state": state.to_dict()}]),
        encoding="utf-8",
    )

    report = audit_human_required_inventory(inventory)

    assert report["counts"] == {"not_human_required": 1}
    assert report["posts"][0]["repair"] is None


def test_audit_classifies_valid_ready_and_published_states_as_not_human_required(tmp_path):
    states = []
    base = _state()
    for lifecycle in (LifecycleState.PENDING, LifecycleState.READY, LifecycleState.PUBLISHED):
        states.append({
            "post_id": len(states) + 1,
            "state": WorkState(
                state=lifecycle,
                phase=Phase.MEDIA,
                blocker=None,
                relevance_approved=lifecycle in (LifecycleState.READY, LifecycleState.PUBLISHED),
                retry=base.retry,
                media=base.media,
            ).to_dict(),
        })
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(states), encoding="utf-8")

    report = audit_human_required_inventory(inventory)

    assert report["counts"] == {"not_human_required": 3}
    assert all(item["blocker"] is None for item in report["posts"])


def test_audit_distinguishes_human_required_without_blocker_from_invalid_state(tmp_path):
    base = _state()
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps([
        {
            "post_id": 1,
            "state": WorkState(
                state=LifecycleState.HUMAN_REQUIRED,
                phase=base.phase,
                blocker=None,
                retry=base.retry,
                media=base.media,
            ).to_dict(),
        },
        {"post_id": 2, "state": {"state": "not-a-lifecycle"}},
    ]), encoding="utf-8")

    report = audit_human_required_inventory(inventory)

    assert report["counts"] == {"evidence_insufficient": 2}
    assert report["posts"][0]["signature"] == "human_required_without_blocker"
    assert report["posts"][1]["signature"] == "missing_or_invalid_work_state"


def test_cohort_report_joins_frozen_ids_with_current_state_without_mutation(tmp_path):
    cohort = tmp_path / "cohort.json"
    cohort.write_text(
        json.dumps({
            "version": 1,
            "posts": [
                {"post_id": 10, "original_datetime": "2026-10-01T00:00:00Z", "classification": "historical"},
                {"post_id": 11, "original_datetime": "2026-10-02T00:00:00Z", "classification": "historical"},
            ],
        }),
        encoding="utf-8",
    )
    ready = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
        media=MediaProgress(
            required=2,
            inline=(InlineMedia(20, "https://cdn.test/a.webp", 0), InlineMedia(21, "https://cdn.test/b.webp", 3)),
        ),
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps({"posts": [{
            "post_id": 10,
            "status": "publish",
            "checklist_approved": True,
            "state": ready.to_dict(),
        }]}),
        encoding="utf-8",
    )

    report = historical_cohort_report(
        cohort,
        inventory,
        now=datetime(2026, 10, 8, tzinfo=timezone.utc),
    )

    assert report["read_only"] is True
    assert report["count"] == 2
    assert report["counts"] == {"ready": 1, "published": 0, "missing_state": 1}
    assert report["posts"][0]["ready"] is True
    assert report["posts"][0]["media"]["accepted"] == 2
    assert report["posts"][0]["wordpress_status"] == "publish"
    assert report["posts"][0]["v2_state"] == "ready"
    assert report["posts"][0]["divergence"] == ["wordpress_publish_v2_ready"]
    assert report["posts"][0]["next_action"] == "v2-reconcile-publication"
    assert report["posts"][0]["media"]["checklist_approved"] is True
    assert report["posts"][1]["next_action"] == "capture_complete_v2_state"


def test_cohort_report_separates_admission_from_scheduler_processability(tmp_path):
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({
        "version": 1,
        "posts": [
            {"post_id": 1, "original_datetime": "2026-10-01T00:00:00Z", "classification": "historical"},
            {"post_id": 2, "original_datetime": "2026-10-01T00:00:00Z", "classification": "historical"},
            {"post_id": 3, "original_datetime": "2026-10-08T00:00:00Z", "classification": "admitted"},
            {"post_id": 4, "original_datetime": "2026-10-08T00:00:00Z", "classification": "admitted"},
            {"post_id": 5, "original_datetime": "2026-10-08T00:00:00Z", "classification": "admitted"},
        ],
    }), encoding="utf-8")
    pending = WorkState(state=LifecycleState.PENDING, phase=Phase.RELEVANCE)
    cooldown = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.RELEVANCE,
        retry=RetryInfo(next_at="2026-10-09T00:00:00+00:00"),
    )
    ready = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"posts": [
        {"post_id": 1, "status": "pending", "state": pending.to_dict()},
        {"post_id": 2, "status": "pending", "state": pending.to_dict()},
        {"post_id": 3, "status": "pending", "state": pending.to_dict()},
        {"post_id": 4, "status": "pending", "state": cooldown.to_dict()},
        {"post_id": 5, "status": "publish", "state": ready.to_dict()},
    ]}), encoding="utf-8")

    report = historical_cohort_report(
        cohort,
        inventory,
        allowlist=(2,),
        now=datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc),
    )
    by_id = {post["post_id"]: post for post in report["posts"]}

    assert by_id[1]["eligibility_real"]["admission_authorized"] is False
    assert by_id[1]["eligibility_real"]["processable"] is False
    assert by_id[1]["eligibility_real"]["reason"] == "historical_not_allowlisted"
    assert by_id[2]["eligibility_real"]["processable"] is True
    assert by_id[3]["eligibility_real"]["processable"] is True
    assert by_id[4]["eligibility_real"]["processable"] is False
    assert by_id[4]["eligibility_real"]["reason"] == "cooldown_active"
    assert by_id[5]["eligibility_real"]["processable"] is False
    assert by_id[5]["eligibility_real"]["reason"] == "wordpress_not_pending"


def test_cohort_report_prefers_canonical_v2_work_state_over_legacy_snapshot(tmp_path):
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({
        "version": 1,
        "posts": [{
            "post_id": 114840,
            "original_datetime": "2026-10-01T00:00:00Z",
            "classification": "admitted",
        }],
    }), encoding="utf-8")
    published = WorkState(
        state=LifecycleState.PUBLISHED,
        phase=Phase.PUBLISH,
        relevance_approved=True,
    )
    ready = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"posts": [{
        "post_id": 114840,
        "wordpress_status": "publish",
        "v2_work_state": published.to_dict(),
        "state": ready.to_dict(),
    }]}), encoding="utf-8")

    report = historical_cohort_report(
        cohort, inventory, now=datetime(2026, 10, 8, tzinfo=timezone.utc)
    )

    post = report["posts"][0]
    assert report["counts"]["ready"] == 0
    assert report["counts"]["published"] == 1
    assert post["v2_state"] == "published"
    assert post["phase"] == "publish"
    assert post["state_source"] == "v2_work_state"
    assert "v2_work_state_conflicts_with_legacy_state" in post["state_conflicts"]


def test_invalid_canonical_state_does_not_fallback_to_legacy_snapshot(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"posts": [{
        "post_id": 114840,
        "v2_work_state": {"state": "not-a-lifecycle"},
        "state": _state().to_dict(),
    }]}), encoding="utf-8")

    report = audit_human_required_inventory(inventory)

    post = report["posts"][0]
    assert post["category"] == "evidence_insufficient"
    assert post["state_source"] == "v2_work_state"
    assert post["state_conflicts"] == ["v2_work_state_invalid_legacy_ignored"]
