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
