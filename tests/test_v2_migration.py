import json
from datetime import datetime, timezone

from unicornio_editor.pipeline_v2.migration import rebase_media_cooldowns
from unicornio_editor.pipeline_v2.model import (
    BlockerCode,
    FeaturedProgress,
    FeaturedStatus,
    InlineMedia,
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
    assert migrated.retry.policy_version == 2
    assert migrated.phase is state.phase
    assert migrated.blocker is state.blocker
    assert migrated.media == state.media

    second = rebase_media_cooldowns(client, apply=True, now=now)
    assert second["candidates"] == 0
    assert len(client.updates) == 1
