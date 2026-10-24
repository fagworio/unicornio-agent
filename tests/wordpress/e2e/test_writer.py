from __future__ import annotations

import json

import pytest

from unicornio_editor.pipeline_v2.transaction_admin import (
    TransactionIsolationError,
    _validate_historical_target_journal,
)


pytestmark = pytest.mark.integration


def _post_events(observation: dict) -> list[dict]:
    return [
        event for event in observation["rest_events"]
        if event.get("phase") == "request"
    ]


def _status_posts(observation: dict) -> list[dict]:
    return [
        event for event in _post_events(observation)
        if event.get("method") == "POST" and event.get("requested_status") == "awaiting_human"
    ]


def test_pending_transitions_to_awaiting_human_with_rest_readback(wordpress_e2e_case):
    observation = wordpress_e2e_case.invoke("pending_transition")
    assert observation["before"]["status"] == "pending"
    assert observation["after"]["status"] == "awaiting_human"
    assert observation["after"]["hermes_work_state"] == wordpress_e2e_case.state_json
    assert observation["writer_error"] is None
    assert observation["writer_result"]["readback"] is True
    assert observation["journal"]["status"] == "committed"
    assert observation["journal"]["readback"] is True
    assert len(_status_posts(observation)) == 1


def test_existing_awaiting_human_without_changes_avoids_post(wordpress_e2e_case):
    observation = wordpress_e2e_case.invoke("noop")
    assert observation["before"]["status"] == "awaiting_human"
    assert observation["after"]["status"] == "awaiting_human"
    assert observation["writer_result"]["wordpress_changed"] is False
    assert observation["writer_result"]["writer_outcome"] == "no_change"
    assert not [event for event in _post_events(observation) if event.get("method") == "POST"]


def test_awaiting_human_metadata_divergence_is_written_and_confirmed(wordpress_e2e_case):
    observation = wordpress_e2e_case.invoke("metadata_divergence")
    assert observation["before"]["status"] == "awaiting_human"
    assert observation["after"]["status"] == "awaiting_human"
    assert observation["writer_result"]["wordpress_changed"] is True
    assert observation["writer_result"]["writer_status"] == "readback_confirmed"
    assert observation["writer_result"]["writer_outcome"] == "changed"
    assert observation["journal"]["readback"] is True
    assert not _status_posts(observation)
    assert len([event for event in _post_events(observation) if event.get("method") == "POST"]) == 1


def test_writer_is_idempotent_across_repeated_real_rest_calls(wordpress_e2e_case):
    first = wordpress_e2e_case.invoke("idempotency")
    posts_after_first = len([event for event in _post_events(first) if event.get("method") == "POST"])
    second = wordpress_e2e_case.invoke("idempotency")
    posts_after_second = len([event for event in _post_events(second) if event.get("method") == "POST"])
    assert first["after"]["status"] == "awaiting_human"
    assert second["after"]["status"] == "awaiting_human"
    assert first["writer_result"]["wordpress_changed"] is True
    assert second["writer_result"]["wordpress_changed"] is False
    assert posts_after_first == posts_after_second == 2


def test_controlled_status_post_failure_is_recorded_without_false_status_change(wordpress_e2e_case):
    observation = wordpress_e2e_case.invoke("post_failure", fault="fail_status_post")
    assert observation["writer_error"]["status_code"] == 503
    assert observation["after"]["status"] == "pending"
    assert observation["journal"]["status"] == "writer_failed"
    assert observation["journal"]["writer_status"] == "possibly_started"
    assert observation["journal"]["readback"] is False
    assert len(_status_posts(observation)) == 1


def test_interruption_after_status_post_before_readback_is_uncertain(wordpress_e2e_case):
    observation = wordpress_e2e_case.invoke("interrupt", fault="interrupt_next_get")
    assert observation["writer_error"]["status_code"] == 503
    assert observation["after"]["status"] == "awaiting_human"
    assert observation["journal"]["status"] == "readback_failed"
    assert observation["journal"]["writer_status"] == "submitted"
    assert observation["journal"]["readback"] is False
    fault_events = [event for event in observation["rest_events"] if event.get("phase") == "fault"]
    assert any(event.get("mode") == "interrupt_next_get" for event in fault_events)


def test_divergent_status_readback_does_not_confirm_writer(wordpress_e2e_case):
    observation = wordpress_e2e_case.invoke("divergent", fault="divergent_readback")
    assert observation["writer_error"] is not None
    assert observation["after"]["status"] == "awaiting_human"
    assert observation["journal"]["status"] == "readback_failed"
    assert observation["journal"]["writer_status"] == "submitted"
    assert observation["journal"]["readback"] is False
    assert any(
        event.get("phase") == "fault" and event.get("mode") == "divergent_readback"
        for event in observation["rest_events"]
    )


def test_historical_journal_without_writer_started_remains_compatible(wordpress_e2e_case):
    post_id = wordpress_e2e_case.post_id("historical")
    wp_state = wordpress_e2e_case.current_post(post_id)
    assert wp_state["status"] == "awaiting_human"
    journal = {
        "post_id": post_id,
        "run_id": "historical-e2e-run",
        "cycle_id": "historical-e2e-cycle",
        "cycle_round": 2,
        "authorization_id": "historical-e2e-authorization",
        "status": "committed",
        "readback": True,
    }
    path = wordpress_e2e_case.root / "legacy-journal.json"
    path.write_text(json.dumps(journal), encoding="utf-8")
    persisted_journal = json.loads(path.read_text(encoding="utf-8"))
    _validate_historical_target_journal(
        persisted_journal,
        post_id=post_id,
        run_id="historical-e2e-run",
        current_authorization_id="current-e2e-authorization",
    )
    wordpress_e2e_case.record_wordpress_readback(
        "historical_journal_compatibility", post_id, persisted_journal,
    )
    with pytest.raises(TransactionIsolationError):
        _validate_historical_target_journal(
            persisted_journal,
            post_id=post_id,
            run_id="historical-e2e-run",
            current_authorization_id="historical-e2e-authorization",
        )


def test_media_registry_v5_matches_confirmed_writer_transaction(wordpress_e2e_case):
    observation = wordpress_e2e_case.invoke("registry")
    registry = observation["registry_v5"]
    assert observation["after"]["status"] == "awaiting_human"
    assert registry["present"] is True
    assert registry["schema_version"] == 5
    operation = registry["media_operation"]
    assert operation["post_id"] == observation["post_id"]
    assert operation["operation_status"] == "completed"
    assert operation["write_started"] == 1
    assert operation["readback_confirmed"] == 1
    assert operation["outcome"] == "human_required"
    assert registry["media_run"]["post_id"] == observation["post_id"]
