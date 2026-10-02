"""Semantic V1/V2 comparison for shadow-mode reports."""

from typing import Any

from .model import OutcomeType


def compare_outcomes(post_id: int, v1_state: str, v1_blocker: str | None, v2_type: OutcomeType, v2_blocker: str | None) -> dict[str, Any]:
    v1_terminal = {"ready": OutcomeType.READY, "skipped": OutcomeType.SKIPPED, "awaiting_human": OutcomeType.HUMAN_REQUIRED}
    expected_type = v1_terminal.get(v1_state, OutcomeType.RETRY)
    equivalent = expected_type is v2_type and (v1_blocker is None or v1_blocker == v2_blocker)
    return {
        "post_id": post_id,
        "v1": {"state": v1_state, "blocker": v1_blocker},
        "v2": {"type": v2_type.value, "blocker": v2_blocker},
        "equivalent": equivalent,
    }
V1_TO_V2_LIFECYCLE = {
    "new": "pending",
    "processing": "pending",
    "blocked": "pending",
    "partial": "pending",
    "uncertain": "pending",
    "ready": "ready",
    "awaiting_human": "human_required",
    "skipped": "skipped",
    "published": "published",
}


def compare_work_state(post_id: int, v1_state: str, actual, *, expected: dict[str, Any], expected_ids: list[int], expected_slots: list[int], expected_action: str, actual_action: str, expected_featured: str | None = None) -> dict[str, Any]:
    """Compare operational equivalence, including resumable media identity."""
    mismatches: list[str] = []
    expected_lifecycle = V1_TO_V2_LIFECYCLE.get(v1_state, "pending")
    state_ok = actual.state.value == expected_lifecycle
    phase_ok = expected.get("phase") in (None, actual.phase.value)
    blocker_ok = expected.get("blocker") in (None, actual.blocker.value if actual.blocker else None)
    ids = [item.media_id for item in actual.media.inline]
    slots = [item.slot for item in actual.media.inline]
    required_ok = "required" not in expected or actual.media.required == expected["required"]
    media_ok = required_ok and actual.media.accepted == expected.get("accepted", actual.media.accepted) and actual.media.missing == expected.get("missing", actual.media.missing) and ids == expected_ids and slots == expected_slots
    featured_ok = expected_featured is None or actual.media.featured.status.value == expected_featured
    action_ok = actual_action == expected_action
    sections = {
        "state": {"v1": v1_state, "v2": actual.state.value, "equivalent": state_ok},
        "phase": {"v2": actual.phase.value, "equivalent": phase_ok},
        "blocker": {"expected": expected.get("blocker"), "actual": actual.blocker.value if actual.blocker else None, "equivalent": blocker_ok},
        "media": {"required": actual.media.required, "accepted": actual.media.accepted, "missing": actual.media.missing, "expected_ids": expected_ids, "actual_ids": ids, "expected_slots": expected_slots, "actual_slots": slots, "equivalent": media_ok},
        "featured": {"expected": expected_featured, "actual": actual.media.featured.status.value, "equivalent": featured_ok},
        "next_action": {"expected": expected_action, "actual": actual_action, "equivalent": action_ok},
    }
    for name, section in sections.items():
        if not section["equivalent"]:
            mismatches.append(name)
    return {"post_id": post_id, **sections, "equivalent": not mismatches, "mismatches": mismatches}
