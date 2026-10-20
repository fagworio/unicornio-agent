"""Bounded V2 cycle orchestration and resumable accounting."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import monotonic
from typing import Any, Callable, Mapping

from .cycle_state import CycleState


@dataclass(frozen=True)
class CycleConfig:
    """Hard limits for one bounded editorial cycle."""

    ready_target: int = 5
    new_batch_size: int = 5
    max_unique_posts: int = 25
    max_total_attempts: int = 50
    max_attempts_per_post_per_cycle: int = 2
    max_stagnant_rounds: int = 3
    max_duration_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.ready_target < 1:
            raise ValueError("ready_target must be positive")
        if self.new_batch_size < 1:
            raise ValueError("new_batch_size must be positive")
        if self.max_unique_posts < 1:
            raise ValueError("max_unique_posts must be positive")
        if self.max_total_attempts < 1:
            raise ValueError("max_total_attempts must be positive")
        if self.max_attempts_per_post_per_cycle < 1:
            raise ValueError("max_attempts_per_post_per_cycle must be positive")
        if self.max_stagnant_rounds < 1:
            raise ValueError("max_stagnant_rounds must be positive")
        if self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")


@dataclass(frozen=True)
class CycleRoundRequest:
    """Authorization and budget supplied to one preview/write round."""

    round_number: int
    batch_size: int
    max_new_posts: int
    seen_post_ids: tuple[int, ...]
    excluded_post_ids: frozenset[int]
    attempts_by_post: Mapping[int, int]


@dataclass(frozen=True)
class CycleRoundResult:
    """Result returned by a round executor after preview and write-mode."""

    preview_selected_ids: tuple[int, ...] = ()
    executed_ids: tuple[int, ...] = ()
    details: tuple[Mapping[str, Any], ...] = ()
    locked: bool = False
    selection_mismatch: bool = False


@dataclass
class CycleReport:
    """Auditable aggregate for one cycle execution."""

    ready_count: int = 0
    ready_post_ids: list[int] = field(default_factory=list)
    unique_posts: int = 0
    attempts_total: int = 0
    rounds: int = 0
    progress_rounds: int = 0
    stagnant_rounds: int = 0
    stop_reason: str = ""
    attempted_post_ids: list[int] = field(default_factory=list)
    attempts_by_post: dict[int, int] = field(default_factory=dict)
    round_reports: list[dict[str, Any]] = field(default_factory=list)
    cycle_id: str | None = None
    resumed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_round_result(value: CycleRoundResult | Mapping[str, Any]) -> CycleRoundResult:
    if isinstance(value, CycleRoundResult):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("round executor must return CycleRoundResult or mapping")
    queue = value.get("queue") if isinstance(value.get("queue"), Mapping) else {}
    preview_ids = value.get("preview_selected_ids")
    if preview_ids is None:
        preview_ids = queue.get("selected_ids", ())
    executed_ids = value.get("executed_ids")
    if executed_ids is None:
        executed_ids = [item.get("post_id") for item in value.get("details", ()) if isinstance(item, Mapping)]
    details = value.get("details", ())
    return CycleRoundResult(
        preview_selected_ids=tuple(int(item) for item in preview_ids or ()),
        executed_ids=tuple(int(item) for item in executed_ids or ()),
        details=tuple(item for item in details if isinstance(item, Mapping)),
        locked=bool(value.get("locked", False)),
        selection_mismatch=bool(value.get("selection_mismatch", False)),
    )


def _detail_progressed(detail: Mapping[str, Any], ready_transition: bool) -> bool:
    if ready_transition or detail.get("progress") is True:
        return True
    if detail.get("media_readback") is True or detail.get("media_readback_status") == "confirmed":
        return True
    if detail.get("applied_media_ids") or detail.get("applied_inline_ids"):
        return True
    initial_phase = detail.get("initial_phase")
    state = detail.get("state")
    final_phase = state.get("phase") if isinstance(state, Mapping) else None
    return bool(initial_phase and final_phase and initial_phase != final_phase)


def _ready_transition(detail: Mapping[str, Any]) -> bool:
    if detail.get("ready_verified") is True:
        return detail.get("initial_state") != "ready"
    state = detail.get("state")
    return bool(
        detail.get("outcome") == "ready"
        and detail.get("readback") is True
        and isinstance(state, Mapping)
        and state.get("state") == "ready"
        and detail.get("initial_state") != "ready"
    )


def run_cycle(
    run_round: Callable[[CycleRoundRequest], CycleRoundResult | Mapping[str, Any]],
    *,
    config: CycleConfig | None = None,
    clock: Callable[[], float] = monotonic,
    state: CycleState | None = None,
    checkpoint: Callable[[CycleState, str, Mapping[str, Any]], None] | None = None,
) -> CycleReport:
    """Run bounded preview-authorized rounds until a stop condition.

    ``run_round`` owns the existing V2 executor integration. It must perform a
    read-only preview, preserve its selected IDs and order, revalidate them in
    write-mode, and return the executed IDs plus readback-backed details.
    """
    config = config or CycleConfig()
    started_at = clock()
    report = CycleReport(
        cycle_id=state.cycle_id if state else None,
        resumed=bool(state and (state.rounds or state.attempts_by_post or state.pending_round)),
    )
    seen: set[int] = set(state.seen_post_ids if state else ())
    ready: set[int] = set(state.ready_post_ids if state else ())
    attempts: dict[int, int] = dict(state.attempts_by_post if state else {})
    stagnant = state.stagnant_rounds if state else 0
    if state:
        report.rounds = state.rounds
        report.progress_rounds = state.progress_rounds
        report.stagnant_rounds = state.stagnant_rounds
        report.round_reports = list(state.round_reports)

    def save(event: str, **fields: Any) -> None:
        if state is None or checkpoint is None:
            return
        state.ready_post_ids = sorted(ready)
        state.seen_post_ids = sorted(seen)
        state.attempts_by_post = dict(sorted(attempts.items()))
        state.rounds = report.rounds
        state.progress_rounds = report.progress_rounds
        state.stagnant_rounds = stagnant
        state.round_reports = list(report.round_reports)
        checkpoint(state, event, fields)

    while True:
        if clock() - started_at >= config.max_duration_seconds:
            report.stop_reason = "duration_limit"
            break
        if len(ready) >= config.ready_target:
            report.stop_reason = "ready_target"
            break
        if sum(attempts.values()) >= config.max_total_attempts:
            report.stop_reason = "attempt_budget"
            break
        if stagnant >= config.max_stagnant_rounds:
            report.stop_reason = "stagnation_limit"
            break

        excluded = frozenset(
            post_id
            for post_id, count in attempts.items()
            if count >= config.max_attempts_per_post_per_cycle
        )
        request = CycleRoundRequest(
            round_number=report.rounds + 1,
            batch_size=min(
                config.new_batch_size,
                config.max_total_attempts - sum(attempts.values()),
            ),
            max_new_posts=max(0, config.max_unique_posts - len(seen)),
            seen_post_ids=tuple(sorted(seen)),
            excluded_post_ids=excluded,
            attempts_by_post=dict(attempts),
        )
        try:
            result = _normalize_round_result(run_round(request))
        except Exception as exc:  # noqa: BLE001 - cycle must report a clean stop
            report.stop_reason = "executor_error"
            report.round_reports.append({
                "round": request.round_number,
                "error": str(exc)[:400],
            })
            break

        if result.locked:
            report.stop_reason = "locked"
            break
        if result.selection_mismatch or result.preview_selected_ids != result.executed_ids:
            report.stop_reason = "selection_mismatch"
            report.round_reports.append({
                "round": request.round_number,
                "preview_selected_ids": list(result.preview_selected_ids),
                "executed_ids": list(result.executed_ids),
            })
            break
        if len(result.executed_ids) != len(set(result.executed_ids)):
            report.stop_reason = "duplicate_selection"
            break
        if len(result.executed_ids) > request.batch_size:
            report.stop_reason = "attempt_budget_violation"
            break
        if any(post_id in request.excluded_post_ids for post_id in result.executed_ids):
            report.stop_reason = "attempt_budget_violation"
            break
        new_ids = set(result.executed_ids) - seen
        if len(new_ids) > request.max_new_posts:
            report.stop_reason = "unique_post_budget_violation"
            break

        if not result.executed_ids:
            report.stop_reason = "no_eligible_work"
            break

        round_progress = False
        round_ready: list[int] = []
        detail_ids = [
            int(detail["post_id"])
            for detail in result.details
            if detail.get("post_id") is not None
        ]
        if (
            len(detail_ids) != len(set(detail_ids))
            or set(detail_ids) != set(result.executed_ids)
        ):
            report.stop_reason = "incomplete_round"
            report.round_reports.append({
                "round": request.round_number,
                "executed_ids": list(result.executed_ids),
                "detail_ids": detail_ids,
            })
            break
        details_by_id = {
            int(detail["post_id"]): detail for detail in result.details
        }
        for post_id in result.executed_ids:
            attempts[post_id] = attempts.get(post_id, 0) + 1
            seen.add(post_id)
            detail = details_by_id.get(post_id, {})
            transition = _ready_transition(detail)
            if transition and post_id not in ready:
                ready.add(post_id)
                round_ready.append(post_id)
            round_progress = _detail_progressed(detail, transition) or round_progress

        report.rounds += 1
        if round_progress:
            stagnant = 0
            report.progress_rounds += 1
        else:
            stagnant += 1
        report.stagnant_rounds = stagnant
        report.round_reports.append({
            "round": report.rounds,
            "preview_selected_ids": list(result.preview_selected_ids),
            "executed_ids": list(result.executed_ids),
            "ready_post_ids": round_ready,
            "progress": round_progress,
        })
        save(
            "round_completed",
            round_number=report.rounds,
            report=report.round_reports[-1],
        )

    report.ready_post_ids = sorted(ready)
    report.ready_count = len(ready)
    report.unique_posts = len(seen)
    report.attempts_total = sum(attempts.values())
    report.attempts_by_post = dict(sorted(attempts.items()))
    report.attempted_post_ids = sorted(seen)
    save("cycle_finished", stop_reason=report.stop_reason)
    return report


__all__ = [
    "CycleConfig",
    "CycleReport",
    "CycleRoundRequest",
    "CycleRoundResult",
    "CycleState",
    "run_cycle",
]
