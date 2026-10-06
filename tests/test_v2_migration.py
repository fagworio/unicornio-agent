import json
from datetime import datetime, timezone

from unicornio_editor.pipeline_v2.migration import (
    repair_known_terminal_states,
    repair_historical_media_human_required,
    rebase_editorial_retries,
    rebase_media_cooldowns,
)
from unicornio_editor.pipeline_v2.model import (
    BlockerCode,
    FeaturedProgress,
    FeaturedStatus,
    InlineMedia,
    LifecycleState,
    MediaProgress,
    Phase,
    RetryInfo,
    WorkState,
)


class Client:
    def __init__(self, posts):
        self.posts = {post["id"]: post for post in posts}
        self.updates = []

    def list_pending(self, *, page, per_page, status="pending"):
        return list(self.posts.values()) if page == 1 else []

    def get_post(self, post_id):
        return self.posts[post_id]

    def update_post(self, post_id, payload):
        self.updates.append((post_id, payload))
        self.posts[post_id]["meta"] = payload["meta"]
        return self.posts[post_id]


def _post(post_id, state):
    return {"id": post_id, "status": "pending", "meta": {"_hermes_work_state": json.dumps(state.to_dict())}}


def test_rebase_media_cooldowns_is_narrow_idempotent_and_preserves_state():
    now = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
    state = WorkState(
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        retry=RetryInfo(attempts=5, no_progress=1, next_at="2026-10-06T23:00:00+00:00"),
        relevance_approved=True,
        media=MediaProgress(
            required=2,
            inline=(InlineMedia(17, "https://cdn.test/a.webp", 0),),
            featured=FeaturedProgress(FeaturedStatus.VALID, 19, "https://cdn.test/f.webp"),
        ),
    )
    client = Client([
        _post(1, state),
        _post(2, WorkState(phase=Phase.EDITORIAL, retry=RetryInfo(next_at="2026-10-06T23:00:00+00:00"))),
    ])

    preview = rebase_media_cooldowns(client, now=now)
    assert preview["post_ids"] == [1]
    assert preview["migrated"] == 0
    assert client.updates == []

    result = rebase_media_cooldowns(client, apply=True, now=now)
    assert result["migrated"] == 1
    assert [post_id for post_id, _payload in client.updates] == [1]
    migrated = WorkState.from_dict(json.loads(client.posts[1]["meta"]["_hermes_work_state"]))
    assert migrated.retry.attempts == 5
    assert migrated.retry.no_progress == 1
    assert migrated.retry.next_at == "2026-10-06T20:00:00+00:00"
    assert migrated.retry.policy_version == 3
    assert migrated.retry.phase_attempts == 0
    assert migrated.phase is state.phase
    assert migrated.blocker is state.blocker
    assert migrated.media == state.media

    second = rebase_media_cooldowns(client, apply=True, now=now)
    assert second["candidates"] == 0
    assert len(client.updates) == 1


def test_rebase_editorial_retries_resets_only_phase_budget():
    now = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
    state = WorkState(
        phase=Phase.EDITORIAL,
        blocker=BlockerCode.TEXT_QUALITY,
        retry=RetryInfo(
            attempts=7,
            no_progress=2,
            next_at="2026-10-06T23:00:00+00:00",
            phase_attempts=4,
        ),
        relevance_approved=True,
        media=MediaProgress(
            required=2,
            inline=(InlineMedia(17, "https://cdn.test/a.webp", 0),),
        ),
    )
    client = Client([_post(9, state)])

    result = rebase_editorial_retries(client, apply=True, now=now)

    assert result["migrated"] == 1
    migrated = WorkState.from_dict(json.loads(client.posts[9]["meta"]["_hermes_work_state"]))
    assert migrated.retry.attempts == 7
    assert migrated.retry.no_progress == 2
    assert migrated.retry.phase_attempts == 0
    assert migrated.retry.policy_version == 3
    assert migrated.retry.next_at == "2026-10-06T20:00:00+00:00"
    assert migrated.media == state.media


def test_historical_repair_reopens_only_exact_audited_media_states():
    now = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
    state = WorkState(
        state=LifecycleState.HUMAN_REQUIRED,
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        retry=RetryInfo(
            attempts=8,
            no_progress=2,
            next_at="2026-10-07T01:00:00+00:00",
            policy_version=3,
            phase_attempts=1,
        ),
        relevance_approved=True,
        media=MediaProgress(required=2, inline=(InlineMedia(17, "https://cdn.test/a.webp", 0),)),
    )
    unrelated = WorkState(
        state=LifecycleState.HUMAN_REQUIRED,
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        retry=RetryInfo(attempts=8, no_progress=2, policy_version=3, phase_attempts=1),
    )
    client = Client([_post(114835, state), _post(1, unrelated)])

    preview = repair_historical_media_human_required(client, now=now)
    assert preview["post_ids"] == [114835]
    assert preview["media_loss_requires_evidence"] == [114949, 114984, 114987]
    assert client.updates == []

    result = repair_historical_media_human_required(client, apply=True, now=now)
    assert result["migrated"] == 1
    repaired = WorkState.from_dict(json.loads(client.posts[114835]["meta"]["_hermes_work_state"]))
    assert repaired.state.value == "pending"
    assert repaired.phase is Phase.MEDIA
    assert repaired.retry.attempts == 8
    assert repaired.retry.no_progress == 1
    assert repaired.media == state.media

    second = repair_historical_media_human_required(client, apply=True, now=now)
    assert second["migrated"] == 0
    assert len(client.updates) == 1


def test_known_terminal_repair_reopens_only_three_exact_bug_states():
    now = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
    media_state = WorkState(
        state=LifecycleState.HUMAN_REQUIRED,
        phase=Phase.MEDIA,
        blocker=BlockerCode.MEDIA_DUPLICATE,
        retry=RetryInfo(attempts=8, no_progress=2, phase_attempts=1, policy_version=3),
        relevance_approved=True,
        media=MediaProgress(
            required=4,
            inline=(InlineMedia(1, "https://wp.test/a.webp", 0), InlineMedia(2, "https://wp.test/b.webp", 1), InlineMedia(3, "https://wp.test/c.webp", 2)),
            featured=FeaturedProgress(FeaturedStatus.VALID, 9, "https://wp.test/f.webp"),
        ),
    )
    editorial_state = WorkState(
        state=LifecycleState.HUMAN_REQUIRED,
        phase=Phase.EDITORIAL,
        blocker=BlockerCode.TEXT_QUALITY,
        retry=RetryInfo(attempts=7, no_progress=1, phase_attempts=3, policy_version=3),
        relevance_approved=True,
        media=MediaProgress(required=2, inline=(InlineMedia(4, "https://wp.test/x.webp", 0),)),
    )
    client = Client([
        _post(114840, media_state),
        _post(115002, editorial_state),
        _post(115004, editorial_state),
        _post(1, media_state),
    ])
    preview = repair_known_terminal_states(client, now=now)
    assert preview["post_ids"] == [114840, 115002, 115004]
    assert client.updates == []
    result = repair_known_terminal_states(client, apply=True, now=now)
    assert result["migrated"] == 3
    repaired_media = WorkState.from_dict(json.loads(client.posts[114840]["meta"]["_hermes_work_state"]))
    assert repaired_media.state is LifecycleState.PENDING
    assert repaired_media.phase is Phase.MEDIA
    assert repaired_media.retry.no_progress == 1
    assert repaired_media.media.accepted == 3
    assert repaired_media.media.featured.status is FeaturedStatus.VALID
    for post_id in (115002, 115004):
        repaired_editorial = WorkState.from_dict(json.loads(client.posts[post_id]["meta"]["_hermes_work_state"]))
        assert repaired_editorial.phase is Phase.EDITORIAL
        assert repaired_editorial.retry.phase_attempts == 2
        assert repaired_editorial.retry.attempts == 7
