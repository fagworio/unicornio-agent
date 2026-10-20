"""Production adapter for the resumable V2 cycle orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .cycle import CycleConfig, CycleReport, CycleRoundRequest, CycleRoundResult, run_cycle
from .cycle_state import CycleState, CycleStateStore
from .lock import RunSessionLock
from .model import LifecycleState
from .production import ProductionCandidateReader
from .runtime import run_v2


def _request_dict(request: CycleRoundRequest) -> dict[str, Any]:
    return {
        "round_number": request.round_number,
        "batch_size": request.batch_size,
        "max_new_posts": request.max_new_posts,
        "seen_post_ids": list(request.seen_post_ids),
        "excluded_post_ids": sorted(request.excluded_post_ids),
        "attempts_by_post": {
            str(post_id): int(count)
            for post_id, count in request.attempts_by_post.items()
        },
    }


def _reconcile_completed_round(client, root: Path, selected_ids: list[int]) -> CycleRoundResult | None:
    """Reconcile a round whose write completed before its cycle checkpoint.

    A post-level V2 journal/readback is the authority. If every authorized
    post is already READY, the cycle can account for the round without issuing
    another WordPress write. Mixed state is deliberately rejected so a new
    preview cannot silently replace the original authorization.
    """
    reader = ProductionCandidateReader(client, root)
    contexts = dict(reader.snapshot(page_size=max(10, len(selected_ids))))
    details: list[dict[str, Any]] = []
    for post_id in selected_ids:
        context = contexts.get(int(post_id))
        state = context.get("v2_state") if context else None
        if state is None or state.state is not LifecycleState.READY:
            return None
        details.append({
            "post_id": int(post_id),
            "initial_state": "pending",
            "outcome": "ready",
            "ready_verified": True,
            "readback": True,
            "state": state.to_dict(),
            "progress": True,
            "reconciled": True,
        })
    return CycleRoundResult(
        preview_selected_ids=tuple(selected_ids),
        executed_ids=tuple(selected_ids),
        details=tuple(details),
    )


def _cycle_config_from_config(config) -> CycleConfig:
    return CycleConfig(
        ready_target=int(getattr(config, "v2_cycle_ready_target", 5)),
        new_batch_size=int(getattr(config, "v2_cycle_new_batch_size", 5)),
        max_unique_posts=int(getattr(config, "v2_cycle_max_unique_posts", 25)),
        max_total_attempts=int(getattr(config, "v2_cycle_max_total_attempts", 50)),
        max_attempts_per_post_per_cycle=int(
            getattr(config, "v2_cycle_max_attempts_per_post", 2)
        ),
        max_stagnant_rounds=int(getattr(config, "v2_cycle_max_stagnant_rounds", 3)),
        max_duration_seconds=float(
            getattr(config, "v2_cycle_max_duration_seconds", 3600.0)
        ),
    )


def run_v2_cycle(
    client,
    config,
    root: Path,
    *,
    cycle_config: CycleConfig | None = None,
    fresh: bool = False,
) -> CycleReport | dict[str, Any]:
    """Run or resume one production V2 cycle under the global V2 lock."""
    root = Path(root)
    store = CycleStateStore(root)
    existing = store.load()
    if existing and existing.status == "running":
        if fresh:
            raise ValueError("an interrupted V2 cycle exists; resume it or remove it explicitly")
        state = existing
        cycle_config = CycleConfig(**state.config) if state.config else (cycle_config or _cycle_config_from_config(config))
    else:
        cycle_config = cycle_config or _cycle_config_from_config(config)
        state = store.create(cycle_config.__dict__)

    lock = RunSessionLock(root / "work" / "v2-run.lock")
    if not lock.acquire():
        return {"selected": 0, "locked": True, "stop_reason": "locked"}

    def checkpoint(current: CycleState, event: str, fields: dict[str, Any]) -> None:
        if event == "round_completed":
            store.complete_round(current, report=fields.get("report") or {})
            return
        if event == "cycle_finished":
            stop_reason = str(fields.get("stop_reason") or "")
            current.stop_reason = stop_reason
            # Unsafe executor/authorization failures remain resumable. Normal
            # budget/drainage stops are completed cycles and start fresh next
            # time, while the pending round is retained for a retry.
            if stop_reason in {
                "executor_error",
                "selection_mismatch",
                "incomplete_round",
                "duplicate_selection",
                "attempt_budget_violation",
                "unique_post_budget_violation",
            }:
                current.status = "running"
                store.save(current, event="cycle_paused", stop_reason=stop_reason)
            else:
                store.finish(current, status="completed", stop_reason=stop_reason)
            return
        store.save(current, event=event, **fields)

    def execute_round(request: CycleRoundRequest) -> CycleRoundResult:
        pending = state.pending_round if isinstance(state.pending_round, dict) else None
        pending_ids = (
            [int(value) for value in pending.get("preview_selected_ids")]
            if pending
            and int(pending.get("round_number", -1)) == request.round_number
            and isinstance(pending.get("preview_selected_ids"), list)
            else None
        )
        if pending_ids is not None:
            preview = run_v2(
                client,
                config,
                root,
                limit=request.batch_size,
                preview=True,
                expected_selected_ids=pending_ids,
                retry_post_ids=request.seen_post_ids,
                excluded_post_ids=request.excluded_post_ids,
                max_new_posts=request.max_new_posts,
                lock_held=True,
            )
            if preview.get("selection_mismatch"):
                reconciled = _reconcile_completed_round(client, root, pending_ids)
                if reconciled is not None:
                    return reconciled
                return CycleRoundResult(
                    preview_selected_ids=tuple(pending_ids),
                    selection_mismatch=True,
                )
            selected_ids = [int(value) for value in preview.get("queue", {}).get("selected_ids", ())]
        else:
            store.start_round(
                state,
                round_number=request.round_number,
                request=_request_dict(request),
            )
            preview = run_v2(
                client,
                config,
                root,
                limit=request.batch_size,
                preview=True,
                retry_post_ids=request.seen_post_ids,
                excluded_post_ids=request.excluded_post_ids,
                max_new_posts=request.max_new_posts,
                lock_held=True,
            )
            selected_ids = [int(value) for value in preview.get("queue", {}).get("selected_ids", ())]
            if preview.get("selection_mismatch"):
                return CycleRoundResult(selection_mismatch=True)
            store.authorize_round(state, selected_ids)

        if not selected_ids:
            return CycleRoundResult()

        result = run_v2(
            client,
            config,
            root,
            limit=request.batch_size,
            preview=False,
            expected_selected_ids=selected_ids,
            retry_post_ids=request.seen_post_ids,
            excluded_post_ids=request.excluded_post_ids,
            max_new_posts=request.max_new_posts,
            lock_held=True,
        )
        executed_ids = tuple(
            int(detail["post_id"])
            for detail in result.get("details", ())
            if isinstance(detail, dict) and detail.get("post_id") is not None
        )
        return CycleRoundResult(
            preview_selected_ids=tuple(selected_ids),
            executed_ids=executed_ids,
            details=tuple(
                detail for detail in result.get("details", ())
                if isinstance(detail, dict)
            ),
            locked=bool(result.get("locked")),
            selection_mismatch=bool(result.get("selection_mismatch")),
        )

    try:
        report = run_cycle(
            execute_round,
            config=cycle_config,
            state=state,
            checkpoint=checkpoint,
        )
        try:
            from ..observability import append_telemetry

            append_telemetry(
                root,
                "v2_cycle",
                cycle_id=state.cycle_id,
                stop_reason=report.stop_reason,
                ready=report.ready_count,
                unique_posts=report.unique_posts,
                attempts=report.attempts_total,
                rounds=report.rounds,
            )
        except Exception:
            pass
        return report
    finally:
        lock.release()


__all__ = ["run_v2_cycle"]
