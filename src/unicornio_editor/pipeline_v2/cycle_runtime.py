"""Production adapter for the resumable V2 cycle orchestrator."""

from __future__ import annotations

import json
import sqlite3
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


def _selection_budget_violation(
    selected_ids: list[int],
    *,
    seen_post_ids: list[int] | tuple[int, ...],
    max_new_posts: int,
    batch_size: int,
    excluded_post_ids: list[int] | tuple[int, ...] | set[int] = (),
) -> str | None:
    """Validate admission before the write-mode V2 executor is called."""
    if len(selected_ids) != len(set(selected_ids)):
        return "duplicate_selection"
    if len(selected_ids) > int(batch_size):
        return "attempt_budget_violation"
    if {int(value) for value in selected_ids}.intersection(
        int(value) for value in excluded_post_ids
    ):
        return "attempt_budget_violation"
    new_ids = set(selected_ids) - {int(value) for value in seen_post_ids}
    if len(new_ids) > int(max_new_posts):
        return "unique_post_budget_violation"
    return None


def _round_authorized_at(root: Path, cycle_id: str, round_number: int) -> str | None:
    path = Path(root) / "work" / "v2-cycle" / "journal.jsonl"
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if (
            event.get("event") == "round_authorized"
            and str(event.get("cycle_id") or "") == str(cycle_id)
            and int(event.get("round_number", -1)) == int(round_number)
        ):
            return str(event.get("at") or "") or None
    return None


def _registry_has_round_evidence(
    root: Path,
    selected_ids: list[int],
    *,
    authorized_at: str | None,
) -> dict[str, Any] | None:
    """Find media-registry effects created after this round was authorized."""
    path = Path(root) / "work" / "media_registry.sqlite"
    if not path.is_file():
        return {"media_registry_checked": False, "media_registry_absent": True}
    placeholders = ",".join("?" for _ in selected_ids)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            rows: list[tuple[str, Any, Any]] = []
            for table, timestamp in (
                ("media_runs", "created_at"),
                ("media_decisions", "created_at"),
                ("media_usage", "applied_at"),
            ):
                try:
                    rows.extend(
                        (table, row[0], row[1])
                        for row in db.execute(
                            f"SELECT post_id, {timestamp} FROM {table} WHERE post_id IN ({placeholders})",
                            selected_ids,
                        ).fetchall()
                    )
                except sqlite3.OperationalError:
                    return None
    except (OSError, sqlite3.Error):
        return None
    if not rows:
        return {"media_registry_checked": True, "media_registry_rows": 0}
    if not authorized_at:
        return None
    current_rows = [
        {"table": table, "post_id": int(post_id), "at": str(timestamp or "")}
        for table, post_id, timestamp in rows
        if timestamp and str(timestamp) >= str(authorized_at).replace("T", " ")[:19]
    ]
    if current_rows:
        return None
    return {
        "media_registry_checked": True,
        "media_registry_rows": len(rows),
        "media_registry_current_round_rows": 0,
    }


def _confirm_round_has_no_write(
    client,
    root: Path,
    selected_ids: list[int],
    *,
    cycle_id: str,
    round_number: int,
) -> dict[str, Any] | None:
    """Return evidence for invalidating a round only when no write is known.

    A journal file is treated as possible write evidence, including prepared or
    partial journals.  In that case recovery must remain blocked so the next
    invocation can reconcile each post instead of discarding the authorization.
    """
    journal_dir = Path(root) / "work" / "v2-journal"
    wordpress_statuses: dict[str, str] = {}
    for post_id in selected_ids:
        if (journal_dir / f"{int(post_id)}.json").exists():
            return None
        try:
            post = client.get_post(int(post_id))
        except Exception:
            return None
        if not isinstance(post, dict):
            return None
        status = str(post.get("status") or "")
        if status != "pending":
            return None
        wordpress_statuses[str(int(post_id))] = status
    registry_evidence = _registry_has_round_evidence(
        root,
        selected_ids,
        authorized_at=_round_authorized_at(root, cycle_id, round_number),
    )
    if registry_evidence is None:
        return None
    return {
        "journal_files_absent": True,
        "wordpress_readback_checked": True,
        "wordpress_statuses": wordpress_statuses,
        **registry_evidence,
        "post_ids": [int(post_id) for post_id in selected_ids],
    }


def _reconcile_completed_round(
    client,
    root: Path,
    selected_ids: list[int],
    *,
    expected_cycle_id: str,
    expected_cycle_round: int,
    preauthorized_new_post_ids: tuple[int, ...] = (),
    historical_out_of_budget: bool = False,
) -> CycleRoundResult | None:
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
        try:
            journal_cycle_round = int(journal.get("cycle_round"))
        except (TypeError, ValueError):
            return None
        if (
            str(journal.get("cycle_id") or "") != str(expected_cycle_id)
            or journal_cycle_round != int(expected_cycle_round)
            or state is None
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
        preauthorized_new_post_ids=tuple(preauthorized_new_post_ids),
        historical_reconciliation=historical_out_of_budget,
        historical_reconciliation_info=(
            {
                "reason": "unique_post_budget_violation",
                "authorization_status": "invalid_historical_execution",
                "write_replayed": False,
            }
            if historical_out_of_budget
            else None
        ),
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
        if event == "round_authorization_invalidated":
            report = fields.get("report") or {}
            store.invalidate_round_authorization(
                current,
                reason=str(report.get("reason") or "invalid_authorization"),
                selected_ids=[int(value) for value in report.get("preview_selected_ids", ())],
                evidence=report.get("evidence") or {},
            )
            return
        if event == "round_reconciled_out_of_budget":
            store.reconcile_historical_round(
                current,
                report=fields.get("report") or {},
            )
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
                current.active_started_at = None
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
        authorized_pending = pending_ids is not None
        authorization_request = (
            pending.get("request")
            if authorized_pending and pending and isinstance(pending.get("request"), dict)
            else {}
        )
        authorized_seen_ids = [
            int(value)
            for value in authorization_request.get("seen_post_ids", request.seen_post_ids)
        ]
        authorized_max_new_posts = int(
            authorization_request.get("max_new_posts", request.max_new_posts)
        )
        authorized_batch_size = int(
            authorization_request.get("batch_size", request.batch_size)
        )
        authorized_excluded_ids = authorization_request.get(
            "excluded_post_ids", request.excluded_post_ids
        )
        executor_limit = authorized_batch_size if authorized_pending else request.batch_size
        executor_retry_ids = (
            tuple(authorized_seen_ids)
            if authorized_pending
            else request.seen_post_ids
        )
        executor_excluded_ids = (
            authorized_excluded_ids
            if authorized_pending
            else request.excluded_post_ids
        )
        executor_max_new_posts = (
            authorized_max_new_posts
            if authorized_pending
            else request.max_new_posts
        )
        if pending_ids is not None:
            preview = run_v2(
                client,
                config,
                root,
                limit=executor_limit,
                preview=True,
                expected_selected_ids=pending_ids,
                retry_post_ids=executor_retry_ids,
                excluded_post_ids=executor_excluded_ids,
                max_new_posts=executor_max_new_posts,
                lock_held=True,
                cycle_id=state.cycle_id,
                cycle_round=request.round_number,
            )
            if preview.get("selection_mismatch"):
                budget_violation = _selection_budget_violation(
                    pending_ids,
                    seen_post_ids=authorized_seen_ids,
                    max_new_posts=authorized_max_new_posts,
                    batch_size=authorized_batch_size,
                    excluded_post_ids=authorized_excluded_ids,
                )
                reconciled = _reconcile_completed_round(
                    client,
                    root,
                    pending_ids,
                    expected_cycle_id=state.cycle_id,
                    expected_cycle_round=request.round_number,
                    preauthorized_new_post_ids=(
                        ()
                        if budget_violation == "unique_post_budget_violation"
                        else tuple(
                            int(post_id)
                            for post_id in set(pending_ids) - set(authorized_seen_ids)
                        )
                    ),
                    historical_out_of_budget=(
                        budget_violation == "unique_post_budget_violation"
                    ),
                )
                if reconciled is not None:
                    return reconciled
                if budget_violation:
                    evidence = _confirm_round_has_no_write(
                        client,
                        root,
                        pending_ids,
                        cycle_id=state.cycle_id,
                        round_number=request.round_number,
                    )
                    if evidence is not None:
                        return CycleRoundResult(
                            preview_selected_ids=tuple(pending_ids),
                            authorization_invalidated=True,
                            authorization_invalidation={
                                "reason": budget_violation,
                                "evidence": evidence,
                            },
                        )
                    return CycleRoundResult(
                        preview_selected_ids=tuple(pending_ids),
                        budget_violation=budget_violation,
                    )
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
                cycle_id=state.cycle_id,
                cycle_round=request.round_number,
            )
            selected_ids = [int(value) for value in preview.get("queue", {}).get("selected_ids", ())]
            if preview.get("selection_mismatch"):
                return CycleRoundResult(selection_mismatch=True)

        budget_violation = _selection_budget_violation(
            selected_ids,
            seen_post_ids=authorized_seen_ids,
            max_new_posts=authorized_max_new_posts,
            batch_size=authorized_batch_size,
            excluded_post_ids=authorized_excluded_ids,
        )
        if budget_violation:
            if authorized_pending and budget_violation == "unique_post_budget_violation":
                reconciled = _reconcile_completed_round(
                    client,
                    root,
                    selected_ids,
                    expected_cycle_id=state.cycle_id,
                    expected_cycle_round=request.round_number,
                    historical_out_of_budget=True,
                )
                if reconciled is not None:
                    return reconciled
                evidence = _confirm_round_has_no_write(
                    client,
                    root,
                    selected_ids,
                    cycle_id=state.cycle_id,
                    round_number=request.round_number,
                )
                if evidence is not None:
                    return CycleRoundResult(
                        preview_selected_ids=tuple(selected_ids),
                        authorization_invalidated=True,
                        authorization_invalidation={
                            "reason": budget_violation,
                            "evidence": evidence,
                        },
                    )
            return CycleRoundResult(
                preview_selected_ids=tuple(selected_ids),
                budget_violation=budget_violation,
            )
        if not authorized_pending:
            store.authorize_round(state, selected_ids)

        if not selected_ids:
            return CycleRoundResult()

        result = run_v2(
            client,
            config,
            root,
            limit=executor_limit,
            preview=False,
            expected_selected_ids=selected_ids,
            retry_post_ids=executor_retry_ids,
            excluded_post_ids=executor_excluded_ids,
            max_new_posts=executor_max_new_posts,
            lock_held=True,
            cycle_id=state.cycle_id,
            cycle_round=request.round_number,
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
            budget_violation=(
                str(result.get("budget_violation"))
                if result.get("budget_violation")
                else None
            ),
            preauthorized_new_post_ids=(
                tuple(
                    int(post_id)
                    for post_id in set(selected_ids) - set(authorized_seen_ids)
                )
                if authorized_pending
                else ()
            ),
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
                ready_conversion_rate=report.ready_conversion_rate,
                attempts_per_ready=report.attempts_per_ready,
            )
        except Exception:
            pass
        return report
    finally:
        lock.release()


__all__ = ["run_v2_cycle"]
