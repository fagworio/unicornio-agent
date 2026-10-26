"""Durable state for one bounded V2 cycle.

The post-level V2 journals remain authoritative for WordPress effects.  This
store only records the cycle's admission/attempt accounting and the round
authorization needed to resume safely after a process interruption.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..backup import atomic_write_text
from .epoch_protection import load_epoch_protection


SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CycleState:
    """Serializable checkpoint for an active or completed cycle."""

    cycle_id: str
    status: str = "running"
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    ready_post_ids: list[int] = field(default_factory=list)
    seen_post_ids: list[int] = field(default_factory=list)
    attempts_by_post: dict[int, int] = field(default_factory=dict)
    rounds: int = 0
    progress_rounds: int = 0
    stagnant_rounds: int = 0
    drain_rounds: int = 0
    phase: str = "ADMISSION"
    elapsed_seconds: float = 0.0
    active_started_at: str | None = None
    stop_reason: str = ""
    round_reports: list[dict[str, Any]] = field(default_factory=list)
    pending_round: dict[str, Any] | None = None
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.cycle_id.strip():
            raise ValueError("cycle_id must not be empty")
        if self.status not in {"running", "completed", "failed"}:
            raise ValueError("invalid cycle status")
        if self.phase not in {"ADMISSION", "DRAINING", "COMPLETED"}:
            raise ValueError("invalid cycle phase")
        if self.elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must not be negative")
        self.ready_post_ids = _ids(self.ready_post_ids)
        self.seen_post_ids = _ids(self.seen_post_ids)
        self.attempts_by_post = {
            int(post_id): int(attempts)
            for post_id, attempts in self.attempts_by_post.items()
        }

    @property
    def attempts_total(self) -> int:
        return sum(self.attempts_by_post.values())

    def touch(self) -> None:
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["schema_version"] = SCHEMA_VERSION
        value["attempts_by_post"] = {
            str(post_id): attempts
            for post_id, attempts in sorted(self.attempts_by_post.items())
        }
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CycleState":
        if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported cycle state schema")
        attempts = value.get("attempts_by_post") or {}
        if not isinstance(attempts, Mapping):
            raise ValueError("attempts_by_post must be an object")
        return cls(
            cycle_id=str(value.get("cycle_id") or ""),
            status=str(value.get("status") or "running"),
            started_at=str(value.get("started_at") or _now()),
            updated_at=str(value.get("updated_at") or _now()),
            ready_post_ids=_ids(value.get("ready_post_ids") or []),
            seen_post_ids=_ids(value.get("seen_post_ids") or []),
            attempts_by_post={int(post_id): int(count) for post_id, count in attempts.items()},
            rounds=int(value.get("rounds") or 0),
            progress_rounds=int(value.get("progress_rounds") or 0),
            stagnant_rounds=int(value.get("stagnant_rounds") or 0),
            drain_rounds=int(value.get("drain_rounds") or 0),
            phase=str(value.get("phase") or "ADMISSION"),
            elapsed_seconds=float(value.get("elapsed_seconds") or 0.0),
            active_started_at=str(value["active_started_at"])
            if value.get("active_started_at")
            else None,
            stop_reason=str(value.get("stop_reason") or ""),
            round_reports=[
                dict(item) for item in (value.get("round_reports") or [])
                if isinstance(item, Mapping)
            ],
            pending_round=dict(value["pending_round"])
            if isinstance(value.get("pending_round"), Mapping)
            else None,
            config=dict(value.get("config") or {}),
        )


def _ids(values: Any) -> list[int]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("post id collection must be a list")
    result = [int(value) for value in values]
    if len(result) != len(set(result)):
        raise ValueError("post ids must be unique")
    return result


class CycleStateStore:
    """Atomic checkpoint plus append-only cycle journal."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.directory = self.root / "work" / "v2-cycle"
        self.state_path = self.directory / "state.json"
        self.journal_path = self.directory / "journal.jsonl"

    def load(self) -> CycleState | None:
        if not self.state_path.is_file():
            return None
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return CycleState.from_dict(value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid V2 cycle checkpoint: {self.state_path}") from exc

    def create(self, config: Mapping[str, Any], *, cycle_id: str | None = None) -> CycleState:
        state = CycleState(
            cycle_id=cycle_id or uuid.uuid4().hex,
            config=dict(config),
        )
        self.save(state, event="cycle_started")
        return state

    def save(self, state: CycleState, *, event: str, **fields: Any) -> None:
        state.touch()
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.state_path,
            json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        journal_event = {
            "schema_version": SCHEMA_VERSION,
            "cycle_id": state.cycle_id,
            "event": event,
            "at": state.updated_at,
            **fields,
        }
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(journal_event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def begin_run(self, state: CycleState) -> None:
        """Start a process invocation without charging inter-run downtime.

        ``active_started_at`` describes an invocation that may still be
        running.  A persisted value can also be left behind by a crash, so it
        must never be treated as proof that the process ran during the time
        between two cron invocations.  The cycle persists elapsed execution
        time at checkpoints; the next invocation starts a fresh session clock.
        """
        now = datetime.now(timezone.utc)
        state.active_started_at = now.isoformat()
        self.save(
            state,
            event="cycle_resumed" if state.rounds or state.pending_round else "cycle_run_started",
        )

    def start_round(
        self,
        state: CycleState,
        *,
        round_number: int,
        request: Mapping[str, Any],
        authorization_id: str | None = None,
    ) -> str:
        authorization_id = str(authorization_id or uuid.uuid4().hex)
        protection = load_epoch_protection(self.root)
        state.pending_round = {
            "round_number": int(round_number),
            "request": dict(request),
            "preview_selected_ids": None,
            "authorization_id": authorization_id,
            "epoch_id": protection.epoch_id,
            "epoch_policy_fingerprint": protection.policy_fingerprint,
        }
        self.save(
            state,
            event="round_started",
            round_number=round_number,
            request=dict(request),
            authorization_id=authorization_id,
        )
        return authorization_id

    def authorize_round(self, state: CycleState, selected_ids: list[int] | tuple[int, ...]) -> None:
        if not state.pending_round:
            raise RuntimeError("cannot authorize a round without a pending round")
        protection = load_epoch_protection(self.root)
        if (
            pending_epoch := str(state.pending_round.get("epoch_id") or "")
        ) != str(protection.epoch_id or "") or str(state.pending_round.get("epoch_policy_fingerprint") or "") != protection.policy_fingerprint:
            raise RuntimeError("epoch policy changed since round start")
        protected = sorted(set(int(value) for value in selected_ids).intersection(protection.protected_ids))
        if protected:
            raise RuntimeError(f"epoch-protected posts cannot be authorized: {protected}")
        state.pending_round["preview_selected_ids"] = [int(value) for value in selected_ids]
        self.save(
            state,
            event="round_authorized",
            round_number=state.pending_round["round_number"],
            selected_ids=list(state.pending_round["preview_selected_ids"]),
            authorization_id=str(state.pending_round.get("authorization_id") or ""),
        )

    def ensure_round_authorization_id(self, state: CycleState) -> str:
        """Assign an identity to a legacy pending authorization exactly once."""
        pending = state.pending_round
        if not isinstance(pending, Mapping):
            raise RuntimeError("cannot identify a round without a pending round")
        current = str(pending.get("authorization_id") or "").strip()
        if current:
            return current
        authorization_id = uuid.uuid4().hex
        pending["authorization_id"] = authorization_id
        self.save(
            state,
            event="round_authorization_reissued",
            round_number=int(pending.get("round_number")),
            authorization_id=authorization_id,
        )
        return authorization_id

    def complete_round(self, state: CycleState, *, report: Mapping[str, Any]) -> None:
        round_number = state.pending_round.get("round_number") if state.pending_round else None
        state.pending_round = None
        self.save(state, event="round_completed", round_number=round_number, report=dict(report))

    def invalidate_round_authorization(
        self,
        state: CycleState,
        *,
        reason: str,
        selected_ids: list[int] | tuple[int, ...],
        evidence: Mapping[str, Any],
    ) -> None:
        """Discard only an unexecuted authorization, retaining its audit trail."""
        pending = state.pending_round
        if not isinstance(pending, Mapping):
            raise RuntimeError("cannot invalidate a round without a pending round")
        expected_ids = [int(value) for value in (pending.get("preview_selected_ids") or [])]
        actual_ids = [int(value) for value in selected_ids]
        if expected_ids != actual_ids:
            raise RuntimeError("round authorization does not match the pending selection")
        state.pending_round = None
        self.save(
            state,
            event="round_authorization_invalidated",
            round_number=int(pending.get("round_number")),
            selected_ids=actual_ids,
            executed_ids=[],
            authorization_id=str(pending.get("authorization_id") or ""),
            reason=str(reason),
            evidence=dict(evidence),
        )

    def reconcile_historical_round(
        self,
        state: CycleState,
        *,
        report: Mapping[str, Any],
    ) -> None:
        """Close a committed but out-of-budget round without recounting it."""
        pending = state.pending_round
        if not isinstance(pending, Mapping):
            raise RuntimeError("cannot reconcile without a pending round")
        selected_ids = [int(value) for value in (pending.get("preview_selected_ids") or [])]
        executed_ids = [int(value) for value in (report.get("executed_ids") or [])]
        if selected_ids != executed_ids:
            raise RuntimeError("historical reconciliation must cover the authorized selection")
        state.pending_round = None
        self.save(
            state,
            event="round_reconciled_out_of_budget",
            round_number=int(pending.get("round_number")),
            selected_ids=selected_ids,
            executed_ids=executed_ids,
            authorization_id=str(pending.get("authorization_id") or ""),
            report=dict(report),
        )

    def reconcile_round(self, state: CycleState, *, report: Mapping[str, Any]) -> None:
        """Close a fully reconciled authorization without replaying writes."""
        pending = state.pending_round
        if not isinstance(pending, Mapping):
            raise RuntimeError("cannot reconcile without a pending round")
        selected_ids = [int(value) for value in (pending.get("preview_selected_ids") or [])]
        reconciled_ids = [int(value) for value in (report.get("preview_selected_ids") or selected_ids)]
        if selected_ids != reconciled_ids:
            raise RuntimeError("reconciliation must cover the authorized selection")
        state.pending_round = None
        self.save(
            state,
            event="round_reconciled",
            round_number=int(pending.get("round_number")),
            selected_ids=selected_ids,
            executed_ids=[int(value) for value in (report.get("executed_ids") or [])],
            authorization_id=str(pending.get("authorization_id") or ""),
            report=dict(report),
        )

    def finish(self, state: CycleState, *, status: str, stop_reason: str) -> None:
        state.status = status
        state.stop_reason = stop_reason
        state.phase = "COMPLETED"
        state.pending_round = None
        state.active_started_at = None
        self.save(state, event="cycle_finished", stop_reason=stop_reason)


__all__ = ["CycleState", "CycleStateStore", "SCHEMA_VERSION"]
