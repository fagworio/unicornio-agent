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
from .runtime import _media_effect_evidence, run_v2


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


def _selection_mismatch_evidence(
    selected_ids: list[int],
    preview: dict[str, Any],
) -> dict[str, Any]:
    """Explain why an authorized selection no longer matches the queue."""
    queue = preview.get("queue") if isinstance(preview.get("queue"), dict) else {}
    eligible_ids = {int(value) for value in queue.get("eligible_ids", ())}
    cooldown_ids = {int(value) for value in queue.get("cooldown_ids", ())}
    quarantined_ids = {int(value) for value in queue.get("quarantined_ids", ())}
    excluded_ids = {int(value) for value in queue.get("cycle_excluded_ids", ())}
    actual_ids = [int(value) for value in preview.get("actual_selected_ids", ())]
    policy_error = queue.get("quarantine_policy_error")
    reason_by_post: dict[str, str] = {}
    for post_id in selected_ids:
        if policy_error and post_id not in eligible_ids:
            reason = "quarantine_policy_error"
        elif post_id in quarantined_ids:
            reason = "quarantined"
        elif post_id in cooldown_ids:
            reason = "cooldown_active"
        elif post_id in excluded_ids:
            reason = "attempt_budget_excluded"
        elif post_id not in eligible_ids:
            reason = "not_eligible"
        else:
            reason = "selection_order_changed"
        reason_by_post[str(post_id)] = reason
    return {
        "expected_selected_ids": [int(value) for value in selected_ids],
        "actual_selected_ids": actual_ids,
        "eligible_ids": sorted(eligible_ids),
        "lost_eligibility_ids": sorted(
            post_id for post_id in selected_ids if post_id not in eligible_ids
        ),
        "reason_by_post": reason_by_post,
        "quarantine_policy_error": str(policy_error or ""),
        "selection_drift": bool(queue.get("selection_drift")),
    }


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
    authorization_id: str | None = None,
    selection_evidence: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return evidence for invalidating a round only when no write is known.

    A journal carrying this authorization is treated as possible write
    evidence, including prepared or partial journals. Journals from a
    different authorization are historical evidence and do not block this
    recovery; a legacy journal with the same cycle/round remains ambiguous and
    fails closed.
    """
    journal_dir = Path(root) / "work" / "v2-journal"
    historical_journal_ids: list[int] = []
    wordpress_statuses: dict[str, str] = {}
    for post_id in selected_ids:
        journal_path = journal_dir / f"{int(post_id)}.json"
        if journal_path.exists():
            if not authorization_id:
                return None
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return None
            journal_authorization_id = str(journal.get("authorization_id") or "").strip()
            try:
                same_legacy_round = (
                    not journal_authorization_id
                    and str(journal.get("cycle_id") or "") == str(cycle_id)
                    and int(journal.get("cycle_round", -1)) == int(round_number)
                )
            except (TypeError, ValueError):
                same_legacy_round = False
            if journal_authorization_id == str(authorization_id) or same_legacy_round:
                return None
            historical_journal_ids.append(int(post_id))
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
        "journal_files_absent": not historical_journal_ids,
        "current_authorization_journal_absent": True,
        "wordpress_readback_checked": True,
        "wordpress_statuses": wordpress_statuses,
        "authorization_id": str(authorization_id or ""),
        "historical_journal_ids": historical_journal_ids,
        "selection": dict(selection_evidence or {}),
        **registry_evidence,
        "post_ids": [int(post_id) for post_id in selected_ids],
    }


def _post_no_write_evidence(
    client,
    root: Path,
    post_id: int,
    *,
    cycle_id: str,
    round_number: int,
) -> dict[str, Any] | None:
    """Prove that one post has no effect belonging to the current round."""
    try:
        post = client.get_post(int(post_id))
    except Exception:
        return None
    if not isinstance(post, dict) or str(post.get("status") or "") != "pending":
        return None
    registry_evidence = _registry_has_round_evidence(
        root,
        [int(post_id)],
        authorized_at=_round_authorized_at(root, cycle_id, round_number),
    )
    if registry_evidence is None:
        return None
    return {
        "post_id": int(post_id),
        "wordpress_status": "pending",
        **registry_evidence,
    }


def _journal_correlation(
    journal: dict[str, Any],
    *,
    expected_cycle_id: str,
    expected_cycle_round: int,
    expected_authorization_id: str | None,
) -> str:
    """Classify a journal as current, historical, or unsafe to infer."""
    journal_authorization_id = str(journal.get("authorization_id") or "").strip()
    try:
        has_cycle_round = (
            journal.get("cycle_id") is not None
            and journal.get("cycle_round") is not None
        )
        same_cycle_round = (
            str(journal.get("cycle_id") or "") == str(expected_cycle_id)
            and int(journal.get("cycle_round")) == int(expected_cycle_round)
        )
    except (TypeError, ValueError):
        has_cycle_round = False
        same_cycle_round = False
    if journal_authorization_id:
        if journal_authorization_id != str(expected_authorization_id or ""):
            return "historical"
        return "current" if same_cycle_round else "uncertain"
    if not journal_authorization_id and same_cycle_round:
        return "current"
    if journal_authorization_id or has_cycle_round:
        return "historical"
    return "uncertain"


def _reconciled_detail(
    post_id: int,
    journal: dict[str, Any],
    state,
) -> dict[str, Any] | None:
    if state is None or state.state not in {
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
    pre_writer_recovery = (
        journal.get("status") in {"interrupted_pre_writer", "failed_pre_writer"}
        and journal.get("writer_started") is False
    )
    transaction_readback = bool(
        not pre_writer_recovery
        and journal.get("status") == "committed"
        and journal.get("readback") is True
    )
    return {
        "post_id": int(post_id),
        "initial_state": "pending",
        "outcome": outcome,
        "ready_verified": state.state is LifecycleState.READY,
        # ``readback`` is retained as the historical compatibility flag.  The
        # explicit fields below distinguish reading current WordPress state
        # from proving a write/readback for this transaction.
        "readback": True,
        "wordpress_state_readback": True,
        "write_started": not pre_writer_recovery,
        "transaction_readback": transaction_readback,
        "write_readback_confirmed": transaction_readback,
        "state": state.to_dict(),
        "media_readback": media_readback,
        "media_readback_status": journal.get("media_readback_status"),
        "progress": bool(
            media_readback
            or state.state is not LifecycleState.PENDING
            or state.phase.value != "relevance"
        ),
        "detail": journal.get("detail"),
        "reconciled": True,
    }


def _reconcile_completed_round(
    client,
    root: Path,
    selected_ids: list[int],
    *,
    expected_cycle_id: str,
    expected_cycle_round: int,
    expected_authorization_id: str | None = None,
    preauthorized_new_post_ids: tuple[int, ...] = (),
    historical_out_of_budget: bool = False,
) -> CycleRoundResult | None:
    """Reconcile each authorized post without replaying any write."""
    reader = ProductionCandidateReader(client, root)
    contexts = dict(reader.snapshot(page_size=max(10, len(selected_ids))))
    details: list[dict[str, Any]] = []
    executed_ids: list[int] = []
    unresolved: list[dict[str, Any]] = []
    has_journal_evidence = False
    journal_dir = Path(root) / "work" / "v2-journal"

    for post_id in selected_ids:
        context = contexts.get(int(post_id))
        if context is None:
            try:
                post = client.get_post(int(post_id))
                if isinstance(post, dict):
                    context = reader.context_loader.load(post)
                    context["v2_state"] = reader._recovered_state(
                        int(post_id), reader.state_store.load(int(post_id)), context
                    )
            except Exception:
                context = None
        state = context.get("v2_state") if context else None
        journal_path = journal_dir / f"{int(post_id)}.json"
        if not journal_path.is_file():
            no_write = _post_no_write_evidence(
                client,
                root,
                int(post_id),
                cycle_id=expected_cycle_id,
                round_number=expected_cycle_round,
            )
            if no_write is not None:
                details.append({
                    "post_id": int(post_id),
                    "initial_state": "pending",
                    "outcome": "not_executed",
                    "readback": True,
                    "wordpress_state_readback": True,
                    "write_started": False,
                    "transaction_readback": False,
                    "write_readback_confirmed": False,
                    "progress": False,
                    "reconciled": True,
                    "no_write_evidence": no_write,
                })
            else:
                unresolved.append({"post_id": int(post_id), "reason": "missing_evidence"})
            continue

        has_journal_evidence = True
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            unresolved.append({"post_id": int(post_id), "reason": "journal_unreadable"})
            continue
        correlation = _journal_correlation(
            journal,
            expected_cycle_id=expected_cycle_id,
            expected_cycle_round=expected_cycle_round,
            expected_authorization_id=expected_authorization_id,
        )
        if correlation == "uncertain":
            unresolved.append({"post_id": int(post_id), "reason": "journal_uncorrelated"})
            continue
        if correlation == "historical":
            no_write = _post_no_write_evidence(
                client,
                root,
                int(post_id),
                cycle_id=expected_cycle_id,
                round_number=expected_cycle_round,
            )
            if no_write is not None:
                details.append({
                    "post_id": int(post_id),
                    "initial_state": "pending",
                    "outcome": "not_executed",
                    "readback": True,
                    "wordpress_state_readback": True,
                    "write_started": False,
                    "transaction_readback": False,
                    "write_readback_confirmed": False,
                    "progress": False,
                    "reconciled": True,
                    "historical_journal": True,
                    "no_write_evidence": no_write,
                })
            else:
                unresolved.append({"post_id": int(post_id), "reason": "historical_effect_uncertain"})
            continue
        if journal.get("status") in {"running", "validated"} and (
            journal.get("writer_started") is False
        ):
            media_evidence = _media_effect_evidence(root, int(post_id), journal)
            if media_evidence.get("effects_detected") or media_evidence.get("evidence_status") == "evidence_unavailable":
                unresolved.append({
                    "post_id": int(post_id),
                    "reason": "media_effects_pre_writer",
                    "recovery_classification": "MEDIA_EFFECTS_PRE_WRITER",
                    "media_effect_evidence": media_evidence,
                })
            else:
                # The protocol journal is still live.  Let write-mode perform
                # the normal stale-journal transition after this safety check;
                # do not count an unfinished transaction as reconciled.
                return None
            continue
        if journal.get("status") in {"interrupted_pre_writer", "failed_pre_writer"} and (
            journal.get("resumable") is True
            and journal.get("writer_started") is False
            and journal.get("readback") is not True
        ):
            media_evidence = _media_effect_evidence(root, int(post_id), journal)
            if media_evidence.get("effects_detected") or media_evidence.get("evidence_status") == "evidence_unavailable":
                unresolved.append({
                    "post_id": int(post_id),
                    "reason": "media_effects_pre_writer",
                    "recovery_classification": "MEDIA_EFFECTS_PRE_WRITER",
                    "media_effect_evidence": media_evidence,
                })
                continue
            detail = _reconciled_detail(int(post_id), journal, state)
            if detail is None:
                unresolved.append({"post_id": int(post_id), "reason": "state_readback_invalid"})
                continue
            detail.update({
                "outcome": "retry",
                "ready_verified": False,
                "progress": True,
                "reconciled": True,
                "write_replayed": False,
                "recovery_status": str(journal.get("status")),
                "failure_class": str(journal.get("failure_class") or "process_interrupted"),
            })
            details.append(detail)
            executed_ids.append(int(post_id))
            continue
        if journal.get("status") != "committed" or journal.get("readback") is not True:
            unresolved.append({
                "post_id": int(post_id),
                "reason": "journal_not_committed",
                "journal_status": journal.get("status"),
            })
            continue
        detail = _reconciled_detail(int(post_id), journal, state)
        if detail is None:
            unresolved.append({"post_id": int(post_id), "reason": "state_readback_invalid"})
            continue
        details.append(detail)
        executed_ids.append(int(post_id))

    if unresolved:
        return CycleRoundResult(
            preview_selected_ids=tuple(selected_ids),
            executed_ids=tuple(executed_ids),
            details=tuple(details),
            preauthorized_new_post_ids=tuple(preauthorized_new_post_ids),
            reconciliation_blocked=True,
            reconciliation_blocking_info={
                "authorization_status": "partially_reconciled",
                "write_replayed": False,
                "unresolved_posts": unresolved,
                "reconciled_post_ids": [int(detail["post_id"]) for detail in details],
            },
        )
    if not has_journal_evidence:
        return None
    if historical_out_of_budget and all(detail.get("outcome") != "not_executed" for detail in details):
        return CycleRoundResult(
            preview_selected_ids=tuple(selected_ids),
            executed_ids=tuple(selected_ids),
            details=tuple(details),
            preauthorized_new_post_ids=tuple(preauthorized_new_post_ids),
            historical_reconciliation=True,
            historical_reconciliation_info={
                "reason": "unique_post_budget_violation",
                "authorization_status": "invalid_historical_execution",
                "write_replayed": False,
            },
        )
    return CycleRoundResult(
        preview_selected_ids=tuple(selected_ids),
        executed_ids=tuple(executed_ids),
        details=tuple(details),
        preauthorized_new_post_ids=tuple(preauthorized_new_post_ids),
        reconciled_without_replay=True,
        reconciled_execution=bool(executed_ids),
        reconciliation_info={
            "authorization_status": "fully_reconciled",
            "write_replayed": False,
            "reconciled_post_ids": [int(detail["post_id"]) for detail in details],
            "counted_post_ids": [int(post_id) for post_id in executed_ids],
            "transaction_readback_post_ids": [
                int(detail["post_id"])
                for detail in details
                if detail.get("transaction_readback") is True
            ],
            "state_readback_only_post_ids": [
                int(detail["post_id"])
                for detail in details
                if detail.get("wordpress_state_readback") is True
                and detail.get("transaction_readback") is not True
            ],
            "not_executed_post_ids": [
                int(detail["post_id"])
                for detail in details
                if detail.get("outcome") == "not_executed"
            ],
        },
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
        try:
            from ..observability import append_telemetry

            append_telemetry(
                root,
                "v2_cycle_skipped_locked",
                status="skipped",
                stop_reason="skipped_locked",
                reason="v2_run_lock_busy",
            )
        except Exception:
            pass
        return {
            "selected": 0,
            "locked": True,
            "status": "skipped",
            "stop_reason": "skipped_locked",
            "reason": "v2_run_lock_busy",
        }

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
        if event == "round_reconciled":
            store.reconcile_round(current, report=fields.get("report") or {})
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
                "recovery_uncertain",
                "skipped_locked",
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
        authorization_id = None
        if authorized_pending:
            authorization_id = store.ensure_round_authorization_id(state)
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
                authorization_id=authorization_id,
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
                    expected_authorization_id=authorization_id,
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
                        authorization_id=authorization_id,
                        selection_evidence=_selection_mismatch_evidence(pending_ids, preview),
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
                evidence = _confirm_round_has_no_write(
                    client,
                    root,
                    pending_ids,
                    cycle_id=state.cycle_id,
                    round_number=request.round_number,
                    authorization_id=authorization_id,
                    selection_evidence=_selection_mismatch_evidence(pending_ids, preview),
                )
                if evidence is not None:
                    return CycleRoundResult(
                        preview_selected_ids=tuple(pending_ids),
                        authorization_invalidated=True,
                        authorization_invalidation={
                            "reason": "selection_mismatch",
                            "evidence": evidence,
                        },
                    )
                return CycleRoundResult(
                    preview_selected_ids=tuple(pending_ids),
                    selection_mismatch=True,
                )
            selected_ids = [int(value) for value in preview.get("queue", {}).get("selected_ids", ())]
        else:
            authorization_id = store.start_round(
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
                authorization_id=authorization_id,
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
                    expected_authorization_id=authorization_id,
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
                    authorization_id=authorization_id,
                    selection_evidence=_selection_mismatch_evidence(selected_ids, preview),
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
        if authorized_pending:
            # A pending authorization is already an admitted write plan. If
            # its journals prove that the plan completed, reconcile it before
            # entering write mode so a resumed cron cannot replay WordPress
            # operations merely because the cycle checkpoint lagged behind.
            journal_dir = Path(root) / "work" / "v2-journal"
            if any(
                (journal_dir / f"{int(post_id)}.json").is_file()
                for post_id in pending_ids
            ):
                reconciled = _reconcile_completed_round(
                    client,
                    root,
                    pending_ids,
                    expected_cycle_id=state.cycle_id,
                    expected_cycle_round=request.round_number,
                    expected_authorization_id=authorization_id,
                )
                if reconciled is not None:
                    return reconciled
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
            authorization_id=authorization_id,
        )
        if result.get("journal_conflict"):
            return CycleRoundResult(
                preview_selected_ids=tuple(selected_ids),
                reconciliation_blocked=True,
                reconciliation_blocking_info={
                    "authorization_status": "incomplete_journal",
                    "write_replayed": False,
                    "journal_conflicts": list(result.get("journal_conflicts") or []),
                },
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
                (
                    "v2_cycle_skipped_locked"
                    if report.stop_reason == "skipped_locked"
                    else "v2_cycle"
                ),
                status=("skipped" if report.stop_reason == "skipped_locked" else "completed"),
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
