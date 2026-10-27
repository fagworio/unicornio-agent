from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from PIL import Image

from unicornio_editor.media.registry import connect as connect_registry, record_v2_observation
from unicornio_editor.pipeline_v2.model import (
    BlockerCode,
    InlineMedia,
    LifecycleState,
    MediaProgress,
    Outcome,
    Phase,
    RetryInfo,
    WorkState,
)
from unicornio_editor.pipeline_v2.runner import PipelineRunner
from unicornio_editor.pipeline_v2.scheduler import select


class WordPressStateStore:
    """Persist each runner checkpoint through the real V2 WordPress writer."""

    def __init__(self, case, post_id: int):
        self.case = case
        self.post_id = post_id
        self.run_id = ""
        self.last_writer_result = None

    def load(self, post_id: int) -> WorkState:
        post = self.case.client.get_post(post_id)
        raw = (post.get("meta") or {}).get("_hermes_work_state")
        assert isinstance(raw, str) and raw
        return WorkState.from_dict(json.loads(raw))

    def commit(self, post_id: int, state: WorkState) -> None:
        post = self.case.client.get_post(post_id)
        outcome = Outcome.retry(state.phase, state.blocker, state.retry.next_at)
        self.last_writer_result = self.case.writer.commit(
            post_id,
            {"post": post, "v2_run_id": self.run_id},
            state,
            outcome,
        )


def _checkpoint(post: dict) -> WorkState:
    raw = (post.get("meta") or {}).get("_hermes_work_state")
    assert isinstance(raw, str) and raw
    return WorkState.from_dict(json.loads(raw))


def _content_sha256(post: dict) -> str:
    content = post.get("content") if isinstance(post.get("content"), dict) else {}
    raw = str(content.get("raw") or content.get("rendered") or "")
    return hashlib.sha256(raw.encode()).hexdigest()


def test_visual_identity_retry_persists_and_resumes_after_cooldown(
    wordpress_e2e_case, tmp_path,
):
    case = wordpress_e2e_case
    post_id = case.post_id("visual_retry")
    new_post_id = case.post_id("visual_retry_new")
    before = case._snapshot(case.client.get_post(post_id))
    content_before = _content_sha256(case.client.get_post(post_id))

    initial = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        retry=RetryInfo(attempts=5, no_progress=2),
        relevance_approved=True,
        media=MediaProgress(required=2),
    )
    case.client.update_post(
        post_id,
        {"meta": {"_hermes_work_state": json.dumps(initial.to_dict(), separators=(",", ":"))}},
    )
    assert _checkpoint(case.client.get_post(post_id)) == initial

    pending_visual_check = MediaProgress(required=2, visual_identity_pending=True)
    media_results = iter((pending_visual_check, pending_visual_check))
    stages = {
        "editorial": lambda *_: (_ for _ in ()).throw(AssertionError("persisted editorial must be reused")),
        "media": lambda *_: next(media_results),
        "compose": lambda *_: {},
        "validate": lambda *_: {
            "passed": False,
            "failures": [
                {"gate": "imagens_no_corpo"},
                {"gate": "qualidade_texto", "detail": "controlled editorial failure"},
            ],
        },
    }
    store = WordPressStateStore(case, post_id)
    runner = PipelineRunner(store, stages)
    context = {"editorial": {"decision": "process"}}

    waiting_run_id = f"wp-e2e-visual-wait-{uuid.uuid4().hex}"
    store.run_id = waiting_run_id
    waiting = runner.run_one(post_id, context)
    assert waiting.type.value == "retry"
    assert waiting.blocker is BlockerCode.PROVIDER_ERROR
    assert store.last_writer_result["readback"] is True
    waiting_state = _checkpoint(case.client.get_post(post_id))
    assert waiting_state.media.visual_identity_pending is True
    assert waiting_state.retry.no_progress == 2
    assert waiting_state.retry.next_at is not None
    waiting_snapshot = case._snapshot(case.client.get_post(post_id))
    assert waiting_snapshot["status"] == "pending"
    assert _content_sha256(case.client.get_post(post_id)) == content_before
    record_v2_observation(
        case.root,
        post_id=post_id,
        run_id=waiting_run_id,
        state=waiting_state,
        phase="media",
        outcome=waiting.blocker.value,
        operation_status="completed",
        write_started=True,
        readback_confirmed=True,
    )
    waiting_journal = case._journal_summary(post_id)
    with connect_registry(case.root) as db:
        waiting_operation = db.execute(
            "SELECT operation_status, readback_confirmed, outcome FROM media_operations WHERE run_id=?",
            (waiting_run_id,),
        ).fetchone()
        assert waiting_operation is not None
        assert waiting_operation[0] == "completed"
        assert waiting_operation[1] == 1
        assert waiting_operation[2] == BlockerCode.PROVIDER_ERROR.value

    queue_states = {
        post_id: waiting_state,
        new_post_id: WorkState(),
    }

    class QueueStore:
        def __init__(self, states):
            self.states = states

        def load(self, candidate_id):
            return self.states[candidate_id]

    now = datetime.now(timezone.utc)
    while_cooling = select(
        [(post_id, {}), (new_post_id, {})],
        QueueStore(queue_states),
        limit=1,
        now=now,
    )
    assert [item[0] for item in while_cooling] == [new_post_id]
    cooldown_expiry = datetime.fromisoformat(waiting_state.retry.next_at)
    after_cooldown = select(
        [(post_id, {})],
        QueueStore(queue_states),
        limit=1,
        now=cooldown_expiry + timedelta(seconds=1),
    )
    assert [item[0] for item in after_cooldown] == [post_id]

    prolonged_run_id = f"wp-e2e-visual-still-waiting-{uuid.uuid4().hex}"
    store.run_id = prolonged_run_id
    prolonged = runner.run_one(post_id, context)
    assert prolonged.type.value == "retry"
    assert prolonged.blocker is BlockerCode.PROVIDER_ERROR
    assert store.last_writer_result["readback"] is True
    prolonged_state = _checkpoint(case.client.get_post(post_id))
    assert prolonged_state.media.visual_identity_pending is True
    assert prolonged_state.retry.no_progress == 2
    assert prolonged_state.retry.attempts == waiting_state.retry.attempts + 1
    assert prolonged_state.retry.next_at is not None
    prolonged_snapshot = case._snapshot(case.client.get_post(post_id))
    assert prolonged_snapshot["status"] == "pending"
    assert _content_sha256(case.client.get_post(post_id)) == content_before
    record_v2_observation(
        case.root,
        post_id=post_id,
        run_id=prolonged_run_id,
        state=prolonged_state,
        phase="media",
        outcome=prolonged.blocker.value,
        operation_status="completed",
        write_started=True,
        readback_confirmed=True,
    )
    prolonged_journal = case._journal_summary(post_id)
    queue_states[post_id] = prolonged_state
    second_new_post_during_cooldown = select(
        [(post_id, {}), (new_post_id, {})],
        QueueStore(queue_states),
        limit=1,
        now=datetime.now(timezone.utc),
    )
    assert [item[0] for item in second_new_post_during_cooldown] == [new_post_id]
    second_after_cooldown = select(
        [(post_id, {})],
        QueueStore(queue_states),
        limit=1,
        now=datetime.fromisoformat(prolonged_state.retry.next_at) + timedelta(seconds=1),
    )
    assert [item[0] for item in second_after_cooldown] == [post_id]

    image_path = tmp_path / "visual-recovery.webp"
    Image.new("RGB", (640, 480), (38, 112, 173)).save(image_path, format="WEBP")
    uploaded = case.client.upload_media(
        str(image_path),
        filename=image_path.name,
        alt_text="Synthetic E2E recovery image",
        title="Synthetic E2E recovery image",
    )
    attachment = case.client.get_media(int(uploaded["id"]))
    details = attachment.get("media_details") or {}
    image_bytes = image_path.read_bytes()
    recovered_asset = InlineMedia(
        media_id=int(attachment["id"]),
        media_url=str(attachment["source_url"]),
        slot=0,
        alt_text="Synthetic E2E recovery image",
        width=int(details.get("width") or 0),
        height=int(details.get("height") or 0),
        sha256=hashlib.sha256(image_bytes).hexdigest(),
        phash="0123456789abcdef",
        visual_group_id="e2e:visual-recovery",
        visual_verification={"decision": "DIFFERENT", "confidence": 0.99},
    )
    assert recovered_asset.width > 0 and recovered_asset.height > 0
    stages["media"] = lambda *_: MediaProgress(required=2, inline=(recovered_asset,))

    resumed_run_id = f"wp-e2e-visual-resume-{uuid.uuid4().hex}"
    store.run_id = resumed_run_id
    resumed = runner.run_one(post_id, context)
    assert resumed.type.value == "retry"
    assert resumed.phase is Phase.EDITORIAL
    assert resumed.blocker is BlockerCode.TEXT_QUALITY
    assert store.last_writer_result["readback"] is True
    resumed_state = _checkpoint(case.client.get_post(post_id))
    assert resumed_state.media.visual_identity_pending is False
    assert resumed_state.retry.no_progress == 0
    assert resumed_state.phase is Phase.EDITORIAL
    resumed_snapshot = case._snapshot(case.client.get_post(post_id))
    assert resumed_snapshot["status"] == "pending"
    assert _content_sha256(case.client.get_post(post_id)) == content_before
    record_v2_observation(
        case.root,
        post_id=post_id,
        run_id=resumed_run_id,
        state=resumed_state,
        phase="editorial",
        outcome=resumed.blocker.value,
        operation_status="completed",
        write_started=True,
        readback_confirmed=True,
    )
    with connect_registry(case.root) as db:
        schema = db.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'",
        ).fetchone()
        registered_asset = db.execute(
            "SELECT wordpress_media_id, sha256, phash, visual_group_id FROM media_assets WHERE wordpress_media_id=?",
            (recovered_asset.media_id,),
        ).fetchone()
        resumed_operation = db.execute(
            "SELECT readback_confirmed, outcome FROM media_operations WHERE run_id=?",
            (resumed_run_id,),
        ).fetchone()
        assert schema is not None and int(schema[0]) == 5
        assert registered_asset is not None
        assert registered_asset[0] == recovered_asset.media_id
        assert registered_asset[1] == recovered_asset.sha256
        assert registered_asset[2] == recovered_asset.phash
        assert registered_asset[3] == recovered_asset.visual_group_id
        assert resumed_operation is not None
        assert resumed_operation[0] == 1
        assert resumed_operation[1] == BlockerCode.TEXT_QUALITY.value
        retry_runs = db.execute(
            "SELECT COUNT(*) FROM media_operations WHERE post_id=? AND outcome=? AND readback_confirmed=1",
            (post_id, BlockerCode.PROVIDER_ERROR.value),
        ).fetchone()
        assert retry_runs is not None and retry_runs[0] == 2

    case.observations.extend((
        {
            "scenario": "visual_retry_waiting",
            "post_id": post_id,
            "before": before,
            "after": waiting_snapshot,
            "outcome": waiting.to_dict(),
            "journal": waiting_journal,
            "registry_v5": case._registry_summary(waiting_run_id),
            "new_post_selected_during_cooldown": new_post_id,
        },
        {
            "scenario": "visual_retry_still_waiting",
            "post_id": post_id,
            "before": waiting_snapshot,
            "after": prolonged_snapshot,
            "outcome": prolonged.to_dict(),
            "journal": prolonged_journal,
            "registry_v5": case._registry_summary(prolonged_run_id),
            "new_post_selected_during_cooldown": int(second_new_post_during_cooldown[0][0]),
        },
        {
            "scenario": "visual_retry_resumed",
            "post_id": post_id,
            "before": waiting_snapshot,
            "after": resumed_snapshot,
            "outcome": resumed.to_dict(),
            "journal": case._journal_summary(post_id),
            "registry_v5": case._registry_summary(resumed_run_id),
            "wordpress_attachment_id": recovered_asset.media_id,
        },
    ))
