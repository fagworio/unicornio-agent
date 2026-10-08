import json
from datetime import datetime, timedelta, timezone

import pytest

from unicornio_editor.pipeline_v2.model import (
    BlockerCode,
    LifecycleState,
    MediaProgress,
    Phase,
    RetryInfo,
    WorkState,
)
from unicornio_editor.pipeline_v2.runtime import admit_v2_candidates, load_historical_cohort
from unicornio_editor.pipeline_v2.scheduler import select


CUTOFF = datetime(2026, 10, 7, 1, 30, tzinfo=timezone.utc)


def candidate(post_id, value, state=None):
    return (
        post_id,
        {
            "post": {"id": post_id, "date_gmt": value},
            "date": value,
            "v2_state": state or WorkState(),
        },
    )


def test_admission_excludes_history_inclusive_at_cutoff():
    before = candidate(1, "2026-10-07T01:29:59+00:00")
    exact = candidate(2, "2026-10-07T01:30:00+00:00")
    after = candidate(3, "2026-10-07T01:30:01+00:00")

    admitted, audit = admit_v2_candidates([before, exact, after], CUTOFF)

    assert [post_id for post_id, _context in admitted] == [2, 3]
    assert audit["historical_excluded"] == 1
    assert audit["historical_excluded_ids"] == [1]
    assert audit["admission_configured"] is True


@pytest.mark.parametrize(
    "phase, blocker",
    [
        (Phase.RELEVANCE, None),
        (Phase.EDITORIAL, BlockerCode.TEXT_QUALITY),
        (Phase.MEDIA, BlockerCode.INLINE_MISSING),
    ],
)
def test_admission_is_stable_across_v2_retry_phases(phase, blocker):
    state = WorkState(
        phase=phase,
        blocker=blocker,
        relevance_approved=phase is not Phase.RELEVANCE,
        media=MediaProgress(2) if phase is Phase.MEDIA else MediaProgress(),
    )

    admitted, _audit = admit_v2_candidates(
        [candidate(10, "2026-10-07T01:30:00+00:00", state)], CUTOFF
    )

    assert [post_id for post_id, _context in admitted] == [10]


def test_historical_media_is_excluded_even_if_cooldown_expired():
    state = WorkState(
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        relevance_approved=True,
        media=MediaProgress(2),
    )

    admitted, audit = admit_v2_candidates(
        [candidate(11, "2026-10-07T01:29:59+00:00", state)], CUTOFF
    )

    assert admitted == []
    assert audit["historical_excluded_ids"] == [11]


def test_frozen_historical_cohort_wins_over_mutated_wordpress_date(tmp_path):
    manifest = tmp_path / "historical-cohort.json"
    manifest.write_text(
        json.dumps({
            "version": 1,
            "posts": [{
                "post_id": 115025,
                "original_datetime": "2026-10-07T01:29:59+00:00",
                "classification": "historical",
            }],
        }),
        encoding="utf-8",
    )
    cohort, error = load_historical_cohort(manifest)
    assert error is None
    admitted, audit = admit_v2_candidates(
        [candidate(115025, "2026-10-08T18:00:00+00:00")],
        CUTOFF,
        historical_cohort=cohort,
    )
    assert admitted == []
    assert audit["historical_excluded_ids"] == [115025]
    assert audit["admission_reasons"] == [{
        "post_id": 115025,
        "decision": "excluded",
        "reason": "frozen_historical_cohort",
    }]


def test_invalid_frozen_cohort_fails_closed(tmp_path):
    manifest = tmp_path / "invalid-cohort.json"
    manifest.write_text("{not-json", encoding="utf-8")
    cohort, error = load_historical_cohort(manifest)
    assert cohort == {}
    assert error
    admitted, audit = admit_v2_candidates(
        [candidate(12, "2026-10-08T18:00:00+00:00")],
        CUTOFF,
        cohort_error=error,
    )
    assert admitted == []
    assert audit["admission_reasons"][0]["reason"] == "invalid_historical_cohort"


def test_allowlist_remains_explicit_escape_hatch_when_cohort_is_invalid():
    admitted, audit = admit_v2_candidates(
        [candidate(12, "2026-10-08T18:00:00+00:00")],
        CUTOFF,
        admission_allowlist=(12,),
        cohort_error="invalid historical cohort",
    )
    assert [post_id for post_id, _context in admitted] == [12]
    assert audit["admission_reasons"] == [{
        "post_id": 12,
        "decision": "admitted",
        "reason": "explicit_allowlist",
    }]


def test_explicit_historical_allowlist_bypasses_only_date_cutoff():
    state = WorkState(
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        relevance_approved=True,
        media=MediaProgress(2),
    )
    admitted, audit = admit_v2_candidates(
        [candidate(11, "2026-10-07T01:29:59+00:00", state)],
        CUTOFF,
        (11,),
    )
    assert [post_id for post_id, _context in admitted] == [11]
    assert audit["historical_allowlisted_ids"] == [11]
    assert audit["historical_excluded_ids"] == []


def test_missing_admission_configuration_fails_closed():
    admitted, audit = admit_v2_candidates(
        [candidate(1, "2026-10-07T01:31:00+00:00")], None
    )

    assert admitted == []
    assert audit["admission_configured"] is False
    assert audit["admission_blocked_ids"] == [1]


class Store:
    def __init__(self, states):
        self.states = states

    def load(self, post_id):
        return self.states[post_id]


def test_admission_keeps_new_cooldown_and_terminal_states_out_of_selection():
    now = CUTOFF + timedelta(hours=1)
    states = {
        1: WorkState(phase=Phase.RELEVANCE),
        2: WorkState(
            phase=Phase.MEDIA,
            blocker=BlockerCode.INLINE_MISSING,
            relevance_approved=True,
            retry=RetryInfo(next_at=(now + timedelta(hours=1)).isoformat()),
            media=MediaProgress(2),
        ),
        3: WorkState(
            state=LifecycleState.HUMAN_REQUIRED,
            phase=Phase.MEDIA,
            blocker=BlockerCode.MEDIA_INVALID,
            relevance_approved=True,
            media=MediaProgress(2),
        ),
        4: WorkState(state=LifecycleState.READY, relevance_approved=True),
        5: WorkState(state=LifecycleState.PUBLISHED, relevance_approved=True),
    }
    snapshot = [
        candidate(post_id, "2026-10-07T01:31:00+00:00", state)
        for post_id, state in states.items()
    ]
    admitted, audit = admit_v2_candidates(snapshot, CUTOFF)
    selected = select(admitted, Store(states), limit=5, now=now)

    assert [post_id for post_id, _context in selected] == [1]
    assert audit["historical_excluded"] == 0

    selected_after_cooldown = select(admitted, Store(states), limit=5, now=now + timedelta(hours=2))
    assert 2 in [post_id for post_id, _context in selected_after_cooldown]
    assert 3 not in [post_id for post_id, _context in selected_after_cooldown]
    assert 4 not in [post_id for post_id, _context in selected_after_cooldown]
    assert 5 not in [post_id for post_id, _context in selected_after_cooldown]
