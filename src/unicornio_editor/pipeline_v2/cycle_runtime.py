"""Production adapter for the resumable V2 cycle orchestrator."""

from __future__ import annotations

import json
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

    A post-level V2 journal/readback is the authority. Each authorized post
    is reconstructed independently, so a mixed READY/PENDING round is counted
    without issuing another WordPress write. Missing or non-committed evidence
    fails closed rather than repeating an unsafe write.
    """
    reader = ProductionCandidateReader(client, root)
    contexts = dict(reader.snapshot(page_size=max(10, len(selected_ids))))
    details: list[dict[str, Any]] = []
    for post_id in selected_ids:
        context = contexts.get(int(post_id))
        if context is None:
            # HUMAN_REQUIRED posts are intentionally outside the pending
            # listing. Reconstruct their read-only state directly, but never
            # infer a result when WordPress cannot be read back.
            try:
                post = client.get_post(int(post_id))
                if not isinstance(post, dict):
                    return None
                context = reader.context_loader.load(post)
                context["v2_state"] = reader._recovered_state(
                    int(post_id), reader.state_store.load(int(post_id)), context
                )
            except Exception:
                return None
        state = context.get("v2_state") if context else None
        journal_path = root / "work" / "v2-journal" / f"{int(post_id)}.json"
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if (
            state is None
            or journal.get("status") != "committed"
            or journal.get("readback") is not True
        ):
            return None
        if state.state not in {
            LifecycleState.READY,
            LifecycleState.PENDING,
            LifecycleState.HUMAN_REQUIRED,
            LifecycleState.SKIPPED,
        }:
            return None
        outcome = {
            LifecycleState.READY: "ready",
            LifecycleState.HUMAN_REQUIRED: "human_required",
            LifecycleState.SKIPPED: "skipped",
        }.get(state.state, "retry")
        media_readback = journal.get("media_readback_status") == "confirmed"
        progressed = bool(
            media_readback
            or state.state is not LifecycleState.PENDING
            or state.phase.value != "relevance"
        )
        detail = {
            "post_id": int(post_id),
            "initial_state": "pending",
            "outcome": outcome,
            "ready_verified": state.state is LifecycleState.READY,
            "readback": True,
            "state": state.to_dict(),
            "media_readback": media_readback,
            "media_readback_status": journal.get("media_readback_status"),
            "progress": progressed,
            "detail": journal.get("detail"),
            "reconciled": True,
        }
        details.append(detail)
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
        max_drain_rounds=int(getattr(config, "v2_cycle_max_drain_rounds", 3)),
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
    lock = RunSessionLock(root / "work" / "v2-run.lock")
    if not lock.acquire():
        return {"selected": 0, "locked": True, "stop_reason": "locked"}

    store = CycleStateStore(root)

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
        # No checkpoint read/create is allowed before the global lock. This
        # prevents two Hermes invocations from racing on state.json.
        existing = store.load()
        if existing and existing.status == "running":
            if fresh:
                raise ValueError("an interrupted V2 cycle exists; resume it or remove it explicitly")
            state = existing
            cycle_config = (
                CycleConfig(**{**CycleConfig().__dict__, **state.config})
                if state.config
                else (cycle_config or _cycle_config_from_config(config))
            )
        else:
            cycle_config = cycle_config or _cycle_config_from_config(config)
            state = store.create(cycle_config.__dict__)
        store.begin_run(state)
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
                phase=report.phase,
                drain_rounds=report.drain_rounds,
                elapsed_seconds=report.elapsed_seconds,
            )
        except Exception:
            pass
        return report
    finally:
        lock.release()


__all__ = ["run_v2_cycle"]
