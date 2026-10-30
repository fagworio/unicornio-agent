from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import replace

import pytest

from unicornio_editor.config import Config
from unicornio_editor.media.registry import connect as connect_registry, record_v2_observation
from unicornio_editor.workflow import retry_post
from unicornio_editor.pipeline_v2.cycle_state import CycleState, CycleStateStore
from unicornio_editor.pipeline_v2.transaction_admin import isolate_uncertain_transaction
from unicornio_editor.pipeline_v2.transaction_admin import (
    TransactionIsolationError,
    _validate_historical_target_journal,
)
from unicornio_editor.pipeline_v2.runtime import (
    _verify_acquisition_checkpoint_for_resume,
    _write_v2_media_checkpoint,
    recover_interrupted_journal,
    run_v2,
)
from unicornio_editor.pipeline_v2 import cycle_runtime
from unicornio_editor.pipeline_v2.model import (
    InlineMedia,
    LifecycleState,
    MediaProgress,
    Phase,
    WorkState,
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


def test_retry_history_meta_roundtrip_and_human_to_pending(wordpress_e2e_case):
    case = wordpress_e2e_case
    post_id = case.post_id("retry_history")
    before_post = case.client.get_post(post_id)
    before_content = (before_post.get("content") or {}).get("raw")
    seed_history = [{"operation": "seed", "previous_retry": {"attempts": 2}}]
    seed_json = json.dumps(seed_history, ensure_ascii=False, separators=(",", ":"))
    case.client.update_post(post_id, {"meta": {"_hermes_retry_history": seed_json}})
    seeded = case.client.get_post(post_id)
    assert seeded["meta"]["_hermes_retry_history"] == seed_json

    config = Config(
        content_source="wordpress",
        wordpress_url=case.url,
        wordpress_api_base="/wp-json/wp/v2",
        app_user=case.user,
        app_password=case.password,
        dry_run=False,
        http_timeout=20.0,
    )
    result = retry_post(case.client, config, case.root, post_id)
    after = case.client.get_post(post_id)
    after_meta = after.get("meta") or {}
    history = json.loads(after_meta["_hermes_retry_history"])
    work_state = json.loads(after_meta["_hermes_work_state"])
    assert result["readback"] is True
    assert after["status"] == "pending"
    assert history[0] == seed_history[0]
    assert history[-1]["operation"] == "retry"
    assert work_state["state"] == "pending"
    assert (after.get("content") or {}).get("raw") == before_content

    audit = case._control(f"/wp-json/unicornio-e2e/v1/audit?post_id={post_id}")
    case.observations.append({
        "scenario": "retry_history_roundtrip",
        "post_id": post_id,
        "before": {"status": before_post["status"], "history": seed_json},
        "after": {"status": after["status"], "history": after_meta["_hermes_retry_history"], "work_state": work_state},
        "retry_result": result,
        "content_preserved": (after.get("content") or {}).get("raw") == before_content,
        "rest_events": audit.get("events", []),
    })


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


def test_started_media_registry_checkpoint_recovers_with_real_wordpress_readbacks(
    wordpress_e2e_case,
    tmp_path,
):
    case = wordpress_e2e_case
    post_id = case.post_id("prewriter_media_resume")
    cycle_id = "e2e-cycle-started-media-resume"
    authorization_id = "e2e-auth-started-media-resume"
    run_id = f"e2e-run-{post_id}-started-media-resume"
    from PIL import Image, ImageDraw
    from unicornio_editor.media.visual_identity import fingerprint_path

    uploaded_media = []
    identities = []
    for index, name in enumerate(("prior", "acquired")):
        image_path = tmp_path / f"{name}-media-resume.webp"
        image = Image.new("RGB", (128, 128), (40 + index * 150, 70, 110))
        draw = ImageDraw.Draw(image)
        for row in range(0, 128, 16):
            draw.rectangle(
                (row, (row * (index + 1)) % 112, row + 9, (row * (index + 1)) % 112 + 12),
                fill=(230 - index * 100, 210, 35 + index * 120),
            )
        image.save(image_path, format="WEBP")
        identities.append(fingerprint_path(image_path))
        uploaded_media.append(case.client.upload_media(
            str(image_path),
            filename=image_path.name,
            alt_text=f"{name.title()} recovery fixture",
            title=f"{name.title()} recovery fixture",
        ))
    prior_id, acquired_id = (int(item["id"]) for item in uploaded_media)
    prior_url, acquired_url = (
        str(item["source_url"]) for item in uploaded_media
    )
    assert prior_id > 0 and acquired_id > 0 and prior_id != acquired_id

    prior_identity, acquired_identity = identities
    initial_state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        relevance_approved=True,
        media=MediaProgress(
            required=2,
            inline=(InlineMedia(
                prior_id, prior_url, 0,
                sha256=prior_identity.sha256,
                phash=prior_identity.phash,
                visual_group_id=prior_identity.visual_group_id,
                visual_verification={"decision": "INITIAL", "method": "e2e_fixture"},
            ),),
        ),
    )
    checkpoint_media = MediaProgress(
        required=2,
        inline=(
            InlineMedia(
                prior_id, prior_url, 0,
                sha256=prior_identity.sha256,
                phash=prior_identity.phash,
                visual_group_id=prior_identity.visual_group_id,
                visual_verification={"decision": "INITIAL", "method": "e2e_fixture"},
            ),
            InlineMedia(
                acquired_id, acquired_url, 1,
                sha256=acquired_identity.sha256,
                phash=acquired_identity.phash,
                visual_group_id=acquired_identity.visual_group_id,
                visual_verification={"decision": "DIFFERENT", "method": "e2e_fixture"},
            ),
        ),
    )
    checkpoint_state = replace(initial_state, media=checkpoint_media)
    case.client.update_post(
        post_id,
        {"meta": {"_hermes_work_state": json.dumps(initial_state.to_dict())}},
    )

    journal = {
        "status": "running",
        "post_id": post_id,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "cycle_round": 1,
        "authorization_id": authorization_id,
        "protocol_version": 2,
        "writer_started": False,
        "readback": False,
        "state": initial_state.to_dict(),
    }
    journal_path = case.root / "work" / "v2-journal" / f"{post_id}.json"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(json.dumps(journal, sort_keys=True), encoding="utf-8")
    _write_v2_media_checkpoint(
        case.root,
        post_id,
        checkpoint_media,
        run_id=run_id,
        cycle_id=cycle_id,
        cycle_round=1,
        authorization_id=authorization_id,
    )
    with connect_registry(case.root):
        pass
    for state in (initial_state, checkpoint_state):
        record_v2_observation(
            case.root,
            post_id=post_id,
            run_id=run_id,
            state=state,
            phase=Phase.MEDIA.value,
            outcome="started",
            checkpoint_reference=f"work/v2-journal/{post_id}.json",
            cycle_id=cycle_id,
            cycle_round=1,
            authorization_id=authorization_id,
            operation_status="started",
            write_started=False,
            readback_confirmed=False,
        )
    cycle_store = CycleStateStore(case.root)
    cycle = CycleState(cycle_id=cycle_id, phase="ADMISSION", status="running")
    cycle_store.start_round(
        cycle,
        round_number=1,
        request={"batch_size": 1},
        authorization_id=authorization_id,
    )
    cycle_store.authorize_round(cycle, [post_id])

    def snapshot() -> dict:
        post = case.client.get_post(post_id)
        content = post.get("content") if isinstance(post.get("content"), dict) else {}
        meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
        return {
            "status": str(post.get("status") or ""),
            "content": str(content.get("raw") or content.get("rendered") or ""),
            "featured_media": int(post.get("featured_media") or 0),
            "work_state": str(meta.get("_hermes_work_state") or ""),
        }

    before = snapshot()
    assert before["status"] == "pending"
    assert before["work_state"] == json.dumps(initial_state.to_dict())
    requests = []
    original_request = case.client._request

    def track_request(method, *args, **kwargs):
        requests.append(str(method).upper())
        return original_request(method, *args, **kwargs)

    def reject_duplicate_upload(*_args, **_kwargs):
        raise AssertionError("pre-writer recovery must not upload media")

    case.client._request = track_request
    case.client.upload_media = reject_duplicate_upload
    verification = _verify_acquisition_checkpoint_for_resume(
        case.client,
        case.root,
        post_id,
        journal,
        {"v2_state": checkpoint_state},
    )
    assert verification is not None
    assert verification["wordpress_status"] == "pending"
    assert verification["wordpress_state_matches"] is True
    assert verification["wordpress_readback_media_ids"] == sorted([prior_id, acquired_id])
    recovery = cycle_runtime._reconcile_completed_round(
        case.client,
        case.root,
        [post_id],
        expected_cycle_id=cycle_id,
        expected_cycle_round=1,
        expected_authorization_id=authorization_id,
    )
    assert recovery is not None
    assert recovery.reconciled_without_replay is True
    assert recovery.executed_ids == (post_id,)
    assert recovery.details[0]["outcome"] == "retry"
    assert recovery.details[0]["ready_verified"] is False
    assert recovery.details[0]["recovery_classification"] == "ACCEPTED_MEDIA_CHECKPOINT_RESUMABLE"
    recovered_journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert recovered_journal["status"] == "interrupted_pre_writer"
    assert recovered_journal["resumable"] is True
    assert recovered_journal["recovery_evidence"]["media_ids"] == sorted([prior_id, acquired_id])
    assert recovered_journal["recovery_evidence"]["new_media_ids"] == [acquired_id]
    journal_after_first_recovery = journal_path.read_text(encoding="utf-8")
    repeated_reconciliation = cycle_runtime._reconcile_completed_round(
        case.client,
        case.root,
        [post_id],
        expected_cycle_id=cycle_id,
        expected_cycle_round=1,
        expected_authorization_id=authorization_id,
    )
    assert repeated_reconciliation is not None
    assert repeated_reconciliation.reconciled_without_replay is True
    assert repeated_reconciliation.executed_ids == (post_id,)
    assert repeated_reconciliation.details[0]["ready_verified"] is False
    assert journal_path.read_text(encoding="utf-8") == journal_after_first_recovery
    second_recovery = recover_interrupted_journal(
        case.root,
        post_id,
        apply=True,
        expected_identity={
            "run_id": run_id,
            "cycle_id": cycle_id,
            "cycle_round": "1",
            "authorization_id": authorization_id,
        },
        media_checkpoint_resume=verification,
    )
    assert second_recovery["recovered"] is False
    assert second_recovery["reason"] == "already_recovered"

    for media_id, expected_url in ((prior_id, prior_url), (acquired_id, acquired_url)):
        attachment = case.client.get_media(media_id)
        assert int(attachment.get("id") or 0) == media_id
        assert str(attachment.get("source_url") or "") == expected_url
    after = snapshot()
    assert after == before
    assert acquired_url not in after["content"]
    assert after["featured_media"] != acquired_id
    assert set(requests) == {"GET"}
    with connect_registry(case.root) as db:
        operation = db.execute(
            """SELECT operation_status, write_started, readback_confirmed, outcome
                 FROM media_operations WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        usage = db.execute(
            """SELECT wordpress_media_id, status, applied_at
                 FROM media_usage WHERE post_id=? ORDER BY wordpress_media_id""",
            (post_id,),
        ).fetchall()
    assert tuple(operation) == ("started", 0, 0, "started")
    assert [(int(row[0]), str(row[1]), row[2]) for row in usage] == [
        (prior_id, "accepted", None),
        (acquired_id, "accepted", None),
    ]
    audit = case._control(f"/wp-json/unicornio-e2e/v1/audit?post_id={post_id}")
    case.observations.append({
        "scenario": "started_media_registry_checkpoint_recovery",
        "post_id": post_id,
        "run_id": run_id,
        "before": {"status": before["status"], "featured_media": before["featured_media"]},
        "after": {"status": after["status"], "featured_media": after["featured_media"]},
        "journal": recovered_journal,
        "rest_events": audit.get("events", []),
        "recovery": {
            "reconciled_without_replay": recovery.reconciled_without_replay,
            "executed_ids": list(recovery.executed_ids),
            "detail": recovery.details[0],
        },
        "repeated_reconciliation": {
            "reconciled_without_replay": repeated_reconciliation.reconciled_without_replay,
            "executed_ids": list(repeated_reconciliation.executed_ids),
        },
        "second_recovery": second_recovery,
        "attachment_readback": {
            str(media_id): {"verified": True, "source_url_matches": True}
            for media_id in (prior_id, acquired_id)
        },
        "wordpress_content_unchanged": after["content"] == before["content"],
        "wordpress_writes_during_recovery": requests,
        "media_registry_operation": dict(
            zip(("operation_status", "write_started", "readback_confirmed", "outcome"), operation)
        ),
        "media_usage": [dict(row) for row in usage],
    })
