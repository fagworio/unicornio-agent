from datetime import datetime, timezone, timedelta

from unicornio_editor.pipeline_v2.model import BlockerCode, LifecycleState, Phase, RetryInfo, WorkState
from unicornio_editor.pipeline_v2.scheduler import cooldown_status, select


class Store:
    def __init__(self, states): self.states = states
    def load(self, post_id): return self.states[post_id]


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
