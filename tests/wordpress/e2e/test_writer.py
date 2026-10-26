from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import replace

import pytest

from unicornio_editor.media.registry import connect as connect_registry, record_v2_observation
from unicornio_editor.pipeline_v2.cycle_state import CycleState, CycleStateStore
from unicornio_editor.pipeline_v2.transaction_admin import isolate_uncertain_transaction
from unicornio_editor.pipeline_v2.transaction_admin import (
    TransactionIsolationError,
    _validate_historical_target_journal,
)
from unicornio_editor.pipeline_v2.runtime import run_v2


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


def test_legacy_prewriter_isolation_uses_real_wordpress_readbacks_without_writes(
    wordpress_e2e_case,
    tmp_path,
):
    case = wordpress_e2e_case
    cycle_id = "e2e-cycle-prewriter-isolation"
    authorization_id = "e2e-auth-prewriter-isolation"
    selected = [
        case.post_id("isolation_target"),
        case.post_id("isolation_committed_a"),
        case.post_id("isolation_committed_b"),
        case.post_id("isolation_not_executed_a"),
        case.post_id("isolation_not_executed_b"),
    ]
    target_id, *other_ids = selected
    run_ids = {post_id: f"e2e-run-{post_id}" for post_id in selected}
    state = case.state.to_dict()
    state_by_post = {post_id: case.current_post(post_id) for post_id in selected}

    image_path = tmp_path / "e2e-isolation-evidence.webp"
    image_path.write_bytes(base64.b64decode(
        "UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEAAUAmJaQAA3AA/v89WAAAAA=="
    ))
    uploaded = case.client.upload_media(
        str(image_path),
        filename="e2e-isolation-evidence.webp",
        alt_text="Isolated integration fixture",
        title="Isolated integration fixture",
    )
    media_id = int(uploaded["id"])
    assert media_id > 0

    store = CycleStateStore(tmp_path)
    cycle = CycleState(cycle_id=cycle_id, phase="ADMISSION", status="running")
    cycle.pending_round = {
        "round_number": 1,
        "authorization_id": authorization_id,
        "preview_selected_ids": selected,
        "request": {"batch_size": len(selected)},
    }
    store.save(
        cycle,
        event="round_authorized",
        round_number=1,
        authorization_id=authorization_id,
        selected_ids=selected,
    )
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )

    target_journal = {
        "status": "running",
        "post_id": target_id,
        "run_id": run_ids[target_id],
        "cycle_id": cycle_id,
        "cycle_round": 1,
        "authorization_id": authorization_id,
        "state": state,
        "writer_started": False,
        "readback": False,
        "resumable": False,
        "media_state": "acquisition_incomplete",
        "media_ids": [media_id],
    }
    (journal_dir / f"{target_id}.json").write_text(
        json.dumps(target_journal, sort_keys=True), encoding="utf-8"
    )
    partial_path = tmp_path / "backups" / str(target_id) / "editorial.partial.json"
    partial_path.parent.mkdir(parents=True)
    partial_path.write_text(json.dumps({
        "inline": {"required": 1, "accepted": [{"media_id": media_id, "slot": 0}]},
        "featured": {"media_id": 0},
    }), encoding="utf-8")

    with connect_registry(tmp_path):
        pass
    record_v2_observation(
        tmp_path,
        post_id=target_id,
        run_id=run_ids[target_id],
        state=case.state,
        phase="media",
        outcome="partial_media_unknown",
        cycle_id=cycle_id,
        cycle_round=1,
        authorization_id=authorization_id,
        operation_status="started",
        write_started=False,
        readback_confirmed=False,
    )
    for index, post_id in enumerate(other_ids):
        journal = {
            "post_id": post_id,
            "run_id": run_ids[post_id],
            "cycle_id": cycle_id,
            "cycle_round": 1,
            "authorization_id": authorization_id,
            "state": state,
            "writer_started": index < 2,
            "readback": index < 2,
            "resumable": index >= 2,
            "status": "committed" if index < 2 else "failed_pre_writer",
            "media_state": "accepted_working" if index < 2 else "apply_not_attempted",
        }
        if index >= 2:
            journal.update({
                "media_application_status": "not_attempted",
                "html_readback_status": "not_attempted",
            })
        (journal_dir / f"{post_id}.json").write_text(
            json.dumps(journal, sort_keys=True), encoding="utf-8"
        )
        if index < 2:
            record_v2_observation(
                tmp_path,
                post_id=post_id,
                run_id=run_ids[post_id],
                state=case.state,
                phase="media",
                outcome="human_required",
                cycle_id=cycle_id,
                cycle_round=1,
                authorization_id=authorization_id,
                operation_status="completed",
                write_started=True,
                readback_confirmed=True,
            )

    before = {post_id: case.current_post(post_id) for post_id in selected}
    requests: list[str] = []
    original_request = case.client._request

    def track_request(method, *args, **kwargs):
        requests.append(str(method).upper())
        return original_request(method, *args, **kwargs)

    def reject_upload(*_args, **_kwargs):
        raise AssertionError("transaction isolation must never upload media")

    case.client._request = track_request
    case.client.upload_media = reject_upload
    args = {
        "root": tmp_path,
        "client": case.client,
        "cycle_id": cycle_id,
        "authorization_id": authorization_id,
        "selected_ids": selected,
        "pending_round": 1,
        "post_id": target_id,
        "run_id": run_ids[target_id],
    }
    preview = isolate_uncertain_transaction(**args, apply=False)
    assert preview["status"] == "preview"
    assert preview["classification"] == "UNCERTAIN_ISOLATED"
    target_evidence = preview["evidence"]["isolated_post"]
    assert target_evidence["prewriter_media_isolation"]["eligible"] is True
    assert target_evidence["attachments"]["status"] == "complete"
    assert target_evidence["media_artifacts"]["sidecar"]["present"] is False

    result = isolate_uncertain_transaction(
        **args,
        apply=True,
        preview=preview,
        operator_authorization="synthetic-ephemeral-wordpress-e2e",
    )
    assert result["status"] == "applied"
    assert result["wordpress_writes"] == 0
    assert not (tmp_path / "backups" / str(target_id) / "editorial.partial.v2.json").exists()
    final_state = store.load()
    assert final_state is not None and final_state.pending_round is None
    assert final_state.rounds == 1
    assert final_state.attempts_by_post == {other_ids[0]: 1, other_ids[1]: 1}
    assert final_state.seen_post_ids == [other_ids[0], other_ids[1]]
    assert final_state.round_reports[-1]["unresolved_post_ids"] == [target_id]
    next_cycle_state = store.load()
    assert next_cycle_state is not None
    next_authorization_id = store.start_round(
        next_cycle_state,
        round_number=next_cycle_state.rounds + 1,
        request={
            "batch_size": 4,
            "max_new_posts": 4,
            "seen_post_ids": next_cycle_state.seen_post_ids,
            "excluded_post_ids": [],
            "attempts_by_post": next_cycle_state.attempts_by_post,
        },
    )
    next_round = run_v2(
        case.client,
        replace(case.client.config, v2_admission_allowlist=tuple(selected)),
        tmp_path,
        limit=4,
        preview=True,
        retry_post_ids=next_cycle_state.seen_post_ids,
        excluded_post_ids=(),
        max_new_posts=4,
        cycle_id=next_cycle_state.cycle_id,
        cycle_round=next_cycle_state.rounds + 1,
        authorization_id=next_authorization_id,
    )
    assert next_authorization_id != authorization_id
    assert next_round["queue"]["quarantined_ids"] == [target_id]
    assert target_id not in next_round["queue"]["selected_ids"]
    assert set(next_round["queue"]["selected_ids"]) <= set(other_ids)
    store.authorize_round(next_cycle_state, next_round["queue"]["selected_ids"])
    assert next_cycle_state.pending_round["authorization_id"] == next_authorization_id
    assert target_id not in next_cycle_state.pending_round["preview_selected_ids"]
    after = {post_id: case.current_post(post_id) for post_id in selected}
    assert requests and set(requests) == {"GET"}
    assert after == before
    quarantine = json.loads(
        (tmp_path / "config" / "v2-quarantine.json").read_text(encoding="utf-8")
    )
    assert [item["post_id"] for item in quarantine["posts"]] == [target_id]
    with sqlite3.connect(tmp_path / "work" / "media_registry.sqlite") as db:
        operation = db.execute(
            "SELECT operation_status, write_started, readback_confirmed FROM media_operations WHERE post_id=? AND run_id=?",
            (target_id, run_ids[target_id]),
        ).fetchone()
    assert operation == ("started", 0, 0)

    case.record_wordpress_readback("prewriter_isolation", target_id, target_journal)
    case.observations[-1]["isolation"] = {
        "status": result["status"],
        "classification": preview["classification"],
        "selected_ids": selected,
        "attempts_by_post": final_state.attempts_by_post,
        "wordpress_request_methods": requests,
        "wordpress_state_unchanged": after == before,
        "registry_operation": operation,
        "next_round_authorization_id": next_authorization_id,
        "next_round_selected_ids": next_round["queue"]["selected_ids"],
        "isolated_post_excluded_from_next_round": target_id not in next_round["queue"]["selected_ids"],
    }
