from datetime import datetime, timezone, timedelta

from unicornio_editor.pipeline_v2.model import BlockerCode, LifecycleState, Phase, RetryInfo, WorkState
from unicornio_editor.pipeline_v2.scheduler import cooldown_status, select


class Store:
    def __init__(self, states): self.states = states
    def load(self, post_id): return self.states[post_id]




def test_select_requeues_human_required_only_with_confirmed_manual_reopen():
    state = WorkState(
        state=LifecycleState.HUMAN_REQUIRED,
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        retry=RetryInfo(next_at="2030-01-02T00:00:00+00:00"),
    )
    item = (7, {"post": {"status": "pending", "meta": {
        "_hermes_human_reopened_at": "2030-01-01T00:00:00+00:00",
        "_hermes_human_reopened_from": "awaiting_human",
    }}})
    result = select([item], Store({7: state}), limit=1, now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert [entry[0] for entry in result] == [7]


def test_select_requires_reopen_source_marker():
    state = WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA)
    item = (7, {"post": {"status": "pending", "meta": {
        "_hermes_human_reopened_at": "2030-01-01T00:00:00+00:00",
    }}})
    result = select([item], Store({7: state}), limit=1, now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert result == []


def test_select_keeps_reopened_relevance_work_in_retry_queue():
    state = WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.RELEVANCE)
    item = (7, {"post": {"status": "pending", "meta": {
        "_hermes_human_reopened_at": "2030-01-01T00:00:00+00:00",
        "_hermes_human_reopened_from": "awaiting_human",
    }}})
    result = select(
        [item],
        Store({7: state}),
        limit=1,
        now=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    assert [entry[0] for entry in result] == [7]


def test_select_excludes_terminal_and_future_cooldown():
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    states = {
        1: WorkState(state=LifecycleState.READY, relevance_approved=True),
        2: WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, retry=RetryInfo(next_at="2030-01-02T00:00:00+00:00")),
        3: WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING),
    }
    result = select([(1, {}), (2, {}), (3, {})], Store(states), limit=5, now=now)
    assert [item[0] for item in result] == [3]


def test_select_reserves_one_slot_for_new_pending_when_near_ready_floods_queue():
    states = {i: WorkState(phase=Phase.MEDIA, blocker=BlockerCode.FEATURED_VISION, relevance_approved=True) for i in range(1, 5)}
    states[9] = WorkState()
    result = select([(i, {}) for i in states], Store(states), limit=3, now=datetime.now(timezone.utc))
    assert 9 in [item[0] for item in result]


def test_select_fills_unused_retry_capacity_with_new_posts():
    states = {
        1: WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True),
        2: WorkState(phase=Phase.MEDIA, blocker=BlockerCode.FEATURED_VISION, relevance_approved=True),
        3: WorkState(),
        4: WorkState(),
        5: WorkState(),
        6: WorkState(),
    }
    result = select([(post_id, {}) for post_id in states], Store(states), limit=5)
    assert [item[0] for item in result] == [2, 1, 3, 4, 5]


def test_select_caps_retry_queue_and_keeps_one_new_slot():
    states = {
        **{
            post_id: WorkState(
                phase=Phase.MEDIA,
                blocker=BlockerCode.FEATURED_VISION,
                relevance_approved=True,
            )
            for post_id in range(1, 11)
        },
        11: WorkState(),
    }
    result = select([(post_id, {}) for post_id in states], Store(states), limit=5)
    assert [item[0] for item in result] == [1, 2, 3, 4, 11]


def test_select_treats_seen_relevance_posts_as_retry_and_can_close_new_admissions():
    states = {
        1: WorkState(),
        2: WorkState(),
        3: WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True),
    }
    result = select(
        [(post_id, {}) for post_id in states],
        Store(states),
        limit=2,
        retry_post_ids=(1,),
        max_new_posts=0,
    )
    assert [item[0] for item in result] == [1]


def test_select_counts_external_editorial_retry_against_admission_budget():
    states = {
        1: WorkState(),
        2: WorkState(),
        3: WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True),
    }
    result = select(
        [(post_id, {}) for post_id in states],
        Store(states),
        limit=2,
        retry_post_ids=(1,),
        max_new_posts=1,
    )
    assert [item[0] for item in result] == [1, 3]


def test_select_excludes_retry_cooldown_before_composing_queues():
    states = {
        1: WorkState(
            phase=Phase.MEDIA,
            blocker=BlockerCode.FEATURED_VISION,
            relevance_approved=True,
            retry=RetryInfo(next_at="2030-01-02T00:00:00+00:00"),
        ),
        2: WorkState(),
    }
    result = select(
        [(1, {}), (2, {})],
        Store(states),
        limit=2,
        now=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    assert [item[0] for item in result] == [2]


def test_cooldown_status_normalizes_utc_and_sao_paulo_offsets():
    instant = datetime(2026, 10, 9, 8, 51, 11, tzinfo=timezone.utc)
    assert cooldown_status("2026-10-09T08:51:11+00:00", now=instant)["expired"] is True
    assert cooldown_status("2026-10-09T05:51:11-03:00", now=instant)["expired"] is True
    assert cooldown_status("2026-10-09T08:51:12Z", now=instant)["active"] is True


def test_cooldown_status_accepts_naive_diagnostic_clock_as_utc():
    status = cooldown_status(
        "2026-10-09T08:51:11+00:00",
        now=datetime(2026, 10, 9, 8, 51, 11),
    )
    assert status["expired"] is True
