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
    stop_reason: str = ""
    round_reports: list[dict[str, Any]] = field(default_factory=list)
    pending_round: dict[str, Any] | None = None
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.cycle_id.strip():
            raise ValueError("cycle_id must not be empty")
        if self.status not in {"running", "completed", "failed"}:
            raise ValueError("invalid cycle status")
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

    def start_round(self, state: CycleState, *, round_number: int, request: Mapping[str, Any]) -> None:
        state.pending_round = {
            "round_number": int(round_number),
            "request": dict(request),
            "preview_selected_ids": None,
        }
        self.save(state, event="round_started", round_number=round_number, request=dict(request))

    def authorize_round(self, state: CycleState, selected_ids: list[int] | tuple[int, ...]) -> None:
        if not state.pending_round:
            raise RuntimeError("cannot authorize a round without a pending round")
        state.pending_round["preview_selected_ids"] = [int(value) for value in selected_ids]
        self.save(
            state,
            event="round_authorized",
            round_number=state.pending_round["round_number"],
            selected_ids=list(state.pending_round["preview_selected_ids"]),
        )

    def complete_round(self, state: CycleState, *, report: Mapping[str, Any]) -> None:
        round_number = state.pending_round.get("round_number") if state.pending_round else None
        state.pending_round = None
        self.save(state, event="round_completed", round_number=round_number, report=dict(report))

    def finish(self, state: CycleState, *, status: str, stop_reason: str) -> None:
        state.status = status
        state.stop_reason = stop_reason
        state.pending_round = None
        self.save(state, event="cycle_finished", stop_reason=stop_reason)


__all__ = ["CycleState", "CycleStateStore", "SCHEMA_VERSION"]
