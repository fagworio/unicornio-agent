"""Durable state for one bounded V2 cycle.

The post-level V2 journals remain authoritative for WordPress effects.  This
store only records the cycle's admission/attempt accounting and the round
authorization needed to resume safely after a process interruption.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

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
        self._write_checkpoint(state)
        self._append_event(state, event, **fields)

    def _write_checkpoint(self, state: CycleState) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.state_path,
            json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )

    def _append_event(self, state: CycleState, event: str, **fields: Any) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
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

    def _events(self) -> list[dict[str, Any]]:
        if not self.journal_path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line_number, line in enumerate(self.journal_path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid cycle journal line {line_number}") from exc
            if not isinstance(value, dict):
                raise RuntimeError(f"invalid cycle journal event at line {line_number}")
            events.append(value)
        return events

    @staticmethod
    def _open_authorizations(
        events: list[dict[str, Any]],
        *,
        cycle_id: str,
        stop_before: int | None = None,
    ) -> dict[tuple[int, str], list[int]]:
        open_items: dict[tuple[int, str], list[int]] = {}
        for event in events[:stop_before]:
            event_round = event.get("round_number")
            if event.get("cycle_id") != cycle_id or not isinstance(event_round, int):
                continue
            event_type = event.get("event")
            if event_type == "round_authorized":
                auth_id = str(event.get("authorization_id") or "")
                if auth_id:
                    open_items[(event_round, auth_id)] = [
                        int(value) for value in event.get("selected_ids", [])
                    ]
            elif event_type in {
                "round_completed",
                "round_reconciled",
                "round_reconciled_out_of_budget",
                "round_authorization_invalidated",
            }:
                auth_id = str(event.get("authorization_id") or "")
                if auth_id:
                    open_items.pop((event_round, auth_id), None)
                else:
                    report = event.get("report") if isinstance(event.get("report"), Mapping) else {}
                    reconciled_ids = report.get("preview_selected_ids")
                    for key, selected in list(open_items.items()):
                        if key[0] == event_round and (
                            reconciled_ids is None or list(reconciled_ids) == selected
                        ):
                            open_items.pop(key, None)
        return open_items

    @staticmethod
    def _validate_prepared_authorizations(
        events: list[dict[str, Any]], *, cycle_id: str
    ) -> None:
        for prepared_event in events:
            if (
                prepared_event.get("cycle_id") != cycle_id
                or prepared_event.get("event") != "round_authorization_prepared"
            ):
                continue
            round_number = prepared_event.get("round_number")
            authorization_id = str(prepared_event.get("authorization_id") or "")
            selected_ids = prepared_event.get("selected_ids")
            intent_id = str(prepared_event.get("round_intent_id") or authorization_id)
            if (
                isinstance(round_number, bool)
                or not isinstance(round_number, int)
                or round_number < 1
                or not authorization_id
                or intent_id != authorization_id
                or not isinstance(selected_ids, list)
                or not selected_ids
            ):
                raise RuntimeError("cycle journal contains an invalid prepared authorization")
            matching_authorized = [
                event for event in events
                if event.get("cycle_id") == cycle_id
                and event.get("event") == "round_authorized"
                and event.get("round_number") == round_number
                and event.get("authorization_id") == authorization_id
            ]
            if (
                len(matching_authorized) != 1
                or matching_authorized[0].get("selected_ids") != selected_ids
                or str(matching_authorized[0].get("round_intent_id") or authorization_id) != intent_id
            ):
                raise RuntimeError("prepared cycle authorization has no exact authorized journal event")

    def validate_no_unresolved_authorizations(self, state: CycleState) -> None:
        events = self._events()
        if not events or not any(event.get("cycle_id") == state.cycle_id for event in events):
            raise RuntimeError("cycle journal is missing for checkpoint identity")
        self._validate_prepared_authorizations(events, cycle_id=state.cycle_id)
        unresolved = self._open_authorizations(events, cycle_id=state.cycle_id)
        if unresolved:
            raise RuntimeError("cycle journal contains an unresolved earlier authorization")

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
        if state.pending_round is not None:
            raise RuntimeError("cannot start a new round while another round is pending")
        # The compatibility parameter is stored only as an intent identity;
        # it becomes an authorization only after selected IDs are persisted.
        round_intent_id = str(authorization_id or uuid.uuid4().hex)
        protection = load_epoch_protection(self.root)
        state.pending_round = {
            "round_number": int(round_number),
            "request": dict(request),
            "preview_selected_ids": None,
            "round_intent_id": round_intent_id,
            "authorization_id": None,
            "authorization_status": "preview",
            "epoch_id": protection.epoch_id,
            "epoch_policy_fingerprint": protection.policy_fingerprint,
        }
        self.save(
            state,
            event="round_started",
            round_number=round_number,
            request=dict(request),
            round_intent_id=round_intent_id,
        )
        return round_intent_id

    def authorize_round(self, state: CycleState, selected_ids: list[int] | tuple[int, ...]) -> str:
        if not state.pending_round:
            raise RuntimeError("cannot authorize a round without a pending round")
        normalized_ids = [int(value) for value in selected_ids]
        if not normalized_ids or len(normalized_ids) != len(set(normalized_ids)):
            raise RuntimeError("round authorization requires a non-empty unique selection")
        pending = state.pending_round
        existing_ids = pending.get("preview_selected_ids")
        current_status = str(pending.get("authorization_status") or "")
        if isinstance(existing_ids, list):
            if [int(value) for value in existing_ids] != normalized_ids:
                raise RuntimeError("cannot replace an already persisted round selection")
            if current_status == "authorized":
                return self.validate_round_authorization(state)
            if current_status != "prepared":
                raise RuntimeError("persisted round selection has an invalid authorization state")
            return self.validate_round_authorization(state)
        protection = load_epoch_protection(self.root)
        if (
            pending_epoch := str(state.pending_round.get("epoch_id") or "")
        ) != str(protection.epoch_id or "") or str(state.pending_round.get("epoch_policy_fingerprint") or "") != protection.policy_fingerprint:
            raise RuntimeError("epoch policy changed since round start")
        protected = sorted(set(int(value) for value in selected_ids).intersection(protection.protected_ids))
        if protected:
            raise RuntimeError(f"epoch-protected posts cannot be authorized: {protected}")
        authorization_id = str(pending.get("round_intent_id") or "").strip()
        if not authorization_id:
            raise RuntimeError("round intent identity is missing")
        pending["preview_selected_ids"] = normalized_ids
        pending["authorization_id"] = authorization_id
        pending["authorization_status"] = "prepared"
        self.save(
            state,
            event="round_authorization_prepared",
            round_number=pending["round_number"],
            selected_ids=normalized_ids,
            authorization_id=authorization_id,
            round_intent_id=str(pending["round_intent_id"]),
        )
        self._append_event(
            state,
            "round_authorized",
            round_number=pending["round_number"],
            selected_ids=normalized_ids,
            authorization_id=authorization_id,
            round_intent_id=str(pending["round_intent_id"]),
        )
        pending["authorization_status"] = "authorized"
        state.touch()
        self._write_checkpoint(state)
        return authorization_id

    def ensure_round_authorization_id(self, state: CycleState) -> str:
        """Validate and recover the durable identity of a persisted selection."""
        return self.validate_round_authorization(state)

    def audit_round_authorization(self, state: CycleState) -> dict[str, Any]:
        """Read-only checkpoint/journal correlation for an authorized round."""
        pending = state.pending_round
        if not isinstance(pending, Mapping):
            raise RuntimeError("cycle has no pending round")
        selected_ids = pending.get("preview_selected_ids")
        authorization_id = str(pending.get("authorization_id") or "").strip()
        round_number = int(pending.get("round_number") or 0)
        intent_id = str(pending.get("round_intent_id") or authorization_id)
        if (
            not isinstance(selected_ids, list)
            or not selected_ids
            or not authorization_id
            or not intent_id
            or (pending.get("round_intent_id") and intent_id != authorization_id)
            or round_number != state.rounds + 1
        ):
            raise RuntimeError("pending authorization identity is invalid")
        selected_ids = [int(value) for value in selected_ids]
        events = self._events()
        starts = [
            event for event in events
            if event.get("cycle_id") == state.cycle_id
            and event.get("event") == "round_started"
            and event.get("round_number") == round_number
            and str(event.get("round_intent_id") or event.get("authorization_id") or "") == intent_id
        ]
        if len(starts) != 1:
            raise RuntimeError("pending authorization has no unique round-start event")
        start_index = next(index for index, event in enumerate(events) if event is starts[0])
        if self._open_authorizations(events, cycle_id=state.cycle_id, stop_before=start_index):
            raise RuntimeError("an earlier authorized round remains unresolved")
        prepared = [
            event for event in events
            if event.get("cycle_id") == state.cycle_id
            and event.get("event") == "round_authorization_prepared"
            and event.get("round_number") == round_number
            and event.get("authorization_id") == authorization_id
        ]
        if any(
            event.get("selected_ids") != selected_ids
            or str(event.get("round_intent_id") or authorization_id) != intent_id
            for event in prepared
        ) or len(prepared) > 1:
            raise RuntimeError("prepared journal selection conflicts with checkpoint")
        status = str(pending.get("authorization_status") or "")
        if status in {"prepared", "authorized"} and len(prepared) != 1:
            raise RuntimeError("checkpoint authorization has no matching prepared event")
        authorized = [
            event for event in events
            if event.get("cycle_id") == state.cycle_id
            and event.get("event") == "round_authorized"
            and event.get("round_number") == round_number
            and event.get("authorization_id") == authorization_id
        ]
        if len(authorized) > 1 or any(
            event.get("selected_ids") != selected_ids
            or str(event.get("round_intent_id") or authorization_id) != intent_id
            for event in authorized
        ):
            raise RuntimeError("authorized journal selection conflicts with checkpoint")
        if not authorized and status == "prepared" and len(prepared) == 1:
            return {
                "status": "prepared_recoverable",
                "cycle_id": state.cycle_id,
                "round_number": round_number,
                "authorization_id": authorization_id,
                "selected_ids": selected_ids,
                "writer_started": False,
            }
        if len(authorized) != 1:
            raise RuntimeError("authorized checkpoint has no matching round_authorized event")
        if not prepared and status:
            raise RuntimeError("new authorization checkpoint is missing its prepared event")
        return {
            "status": "authorized",
            "cycle_id": state.cycle_id,
            "round_number": round_number,
            "authorization_id": authorization_id,
            "selected_ids": selected_ids,
        }

    def validate_pre_authorization(self, state: CycleState) -> dict[str, Any]:
        """Prove that a pending round has no persisted or journaled authorization."""
        pending = state.pending_round
        if not isinstance(pending, Mapping):
            raise RuntimeError("cannot validate a round without a pending round")
        if pending.get("preview_selected_ids") is not None:
            raise RuntimeError("round already contains authorization data")
        round_number = int(pending.get("round_number") or 0)
        if round_number < 1 or round_number != state.rounds + 1:
            raise RuntimeError("pending pre-authorization round number diverges from cycle counters")
        intent_id = str(
            pending.get("round_intent_id")
            or pending.get("authorization_id")
            or ""
        )
        events = self._events()
        matches = [
            (index, event)
            for index, event in enumerate(events)
            if event.get("cycle_id") == state.cycle_id
            and event.get("event") == "round_started"
            and event.get("round_number") == round_number
            and str(event.get("round_intent_id") or event.get("authorization_id") or "") == intent_id
        ]
        if len(matches) != 1 or not intent_id:
            raise RuntimeError("pending pre-authorization checkpoint does not match cycle journal")
        journal_request = matches[0][1].get("request")
        checkpoint_request = pending.get("request")
        if not isinstance(journal_request, Mapping) or not isinstance(checkpoint_request, Mapping):
            raise RuntimeError("pending pre-authorization request evidence is incomplete")
        if dict(journal_request) != dict(checkpoint_request):
            raise RuntimeError("pending pre-authorization request diverges from cycle journal")
        start_index = matches[0][0]
        started_at = matches[0][1].get("at")
        try:
            started_dt = datetime.fromisoformat(str(started_at))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("round_started timestamp is invalid") from exc
        if started_dt.tzinfo is None or started_dt.timestamp() > datetime.now(timezone.utc).timestamp() + 60:
            raise RuntimeError("round_started timestamp is missing timezone or in the future")

        open_authorizations = self._open_authorizations(
            events,
            cycle_id=state.cycle_id,
            stop_before=start_index,
        )
        if open_authorizations:
            raise RuntimeError("an earlier authorized round remains unresolved")
        self._validate_prepared_authorizations(
            events[:start_index], cycle_id=state.cycle_id
        )

        later_authorization = any(
            event.get("cycle_id") == state.cycle_id
            and event.get("round_number") == round_number
            and event.get("event") in {
                "round_authorization_prepared",
                "round_authorized",
            }
            for event in events[start_index + 1 :]
        )
        if later_authorization:
            raise RuntimeError("cycle journal contains authorization absent from checkpoint")
        started_epoch = started_dt.timestamp()
        journal_dir = self.root / "work" / "v2-journal"
        changed_post_journals = []
        if journal_dir.is_dir():
            for path in journal_dir.glob("*.json"):
                if path.is_symlink():
                    raise RuntimeError("post journal symlink makes pre-authorization evidence ambiguous")
                if path.stat().st_mtime > started_epoch:
                    changed_post_journals.append(path.stem)
        registry_path = self.root / "work" / "media_registry.sqlite"
        changed_registry_files = []
        for suffix in ("", "-wal"):
            path = Path(f"{registry_path}{suffix}")
            if path.is_symlink():
                raise RuntimeError("media registry symlink makes pre-authorization evidence ambiguous")
            if path.is_file() and path.stat().st_mtime > started_epoch:
                changed_registry_files.append(path.name)
        if changed_post_journals or changed_registry_files:
            raise RuntimeError(
                "post or media evidence changed after round_started: "
                f"journals={changed_post_journals[:20]}, registry={changed_registry_files}"
            )
        if isinstance(pending, dict):
            pending["round_intent_id"] = intent_id
            pending["authorization_id"] = None
            pending["authorization_status"] = "preview"
        return {
            "status": "preauthorization_safe_to_resume",
            "cycle_id": state.cycle_id,
            "round_number": round_number,
            "round_intent_id": intent_id,
            "selected_ids": None,
            "wordpress_writes": 0,
        }

    def validate_round_authorization(self, state: CycleState) -> str:
        """Require a matching durable journal event before a selection can write."""
        pending = state.pending_round
        if not isinstance(pending, Mapping):
            raise RuntimeError("cannot validate a round without a pending round")
        selected_ids = pending.get("preview_selected_ids")
        if not isinstance(selected_ids, list) or not selected_ids:
            raise RuntimeError("pending round does not contain an authorized selection")
        authorization_status = str(pending.get("authorization_status") or "")
        if authorization_status and authorization_status not in {"prepared", "authorized"}:
            raise RuntimeError("pending round authorization status is invalid")
        selected_ids = [int(value) for value in selected_ids]
        round_number = int(pending.get("round_number") or 0)
        if round_number != state.rounds + 1:
            raise RuntimeError("authorized pending round number diverges from cycle counters")
        events = self._events()
        authorization_id = str(pending.get("authorization_id") or "").strip()
        intent_id = str(pending.get("round_intent_id") or authorization_id)
        if pending.get("round_intent_id") and authorization_id and intent_id != authorization_id:
            raise RuntimeError("checkpoint round intent and authorization identity diverge")
        starts = [
            event
            for event in events
            if event.get("cycle_id") == state.cycle_id
            and event.get("event") == "round_started"
            and event.get("round_number") == round_number
            and str(event.get("round_intent_id") or event.get("authorization_id") or "") == intent_id
        ]
        if len(starts) != 1 or not intent_id:
            raise RuntimeError("authorized checkpoint has no matching round-start event")
        start_index = next(index for index, event in enumerate(events) if event is starts[0])
        if self._open_authorizations(
            events,
            cycle_id=state.cycle_id,
            stop_before=start_index,
        ):
            raise RuntimeError("an earlier authorized round remains unresolved")
        if not authorization_id:
            matches = [
                event
                for event in events
                if event.get("cycle_id") == state.cycle_id
                and event.get("event") == "round_authorized"
                and event.get("round_number") == round_number
                and event.get("selected_ids") == selected_ids
            ]
            identities = {str(event.get("authorization_id") or "") for event in matches}
            if len(identities) != 1 or not next(iter(identities), ""):
                raise RuntimeError("pending round has no unique journaled authorization identity")
            authorization_id = next(iter(identities))
            if isinstance(pending, dict):
                pending["authorization_id"] = authorization_id
        prepared = [
            event
            for event in events
            if event.get("cycle_id") == state.cycle_id
            and event.get("event") == "round_authorization_prepared"
            and event.get("round_number") == round_number
            and event.get("authorization_id") == authorization_id
            and event.get("selected_ids") == selected_ids
            and str(event.get("round_intent_id") or authorization_id) == intent_id
        ]
        if len(prepared) != 1:
            # Legacy checkpoints did not have the prepared event.  Accept only
            # a single exact round_authorized event as their provenance.
            if authorization_status in {"prepared", "authorized"}:
                raise RuntimeError("new authorization checkpoint is missing its prepared journal event")
            legacy_authorized = [
                event
                for event in events
                if event.get("cycle_id") == state.cycle_id
                and event.get("event") == "round_authorized"
                and event.get("round_number") == round_number
                and event.get("authorization_id") == authorization_id
                and event.get("selected_ids") == selected_ids
            ]
            if len(legacy_authorized) != 1:
                raise RuntimeError("pending round authorization preparation is missing or divergent")
        authorized = [
            event
            for event in events
            if event.get("cycle_id") == state.cycle_id
            and event.get("event") == "round_authorized"
            and event.get("round_number") == round_number
            and event.get("authorization_id") == authorization_id
        ]
        if len(authorized) > 1 or any(
            event.get("selected_ids") != selected_ids
            or str(event.get("round_intent_id") or authorization_id) != intent_id
            for event in authorized
        ):
            raise RuntimeError("cycle journal authorization conflicts with checkpoint selection")
        if not authorized:
            if not prepared:
                raise RuntimeError("cannot recover authorization without a matching prepared journal event")
            self._append_event(
                state,
                "round_authorized",
                round_number=round_number,
                selected_ids=selected_ids,
                authorization_id=authorization_id,
                round_intent_id=str(pending.get("round_intent_id") or authorization_id),
                recovered=True,
            )
        if isinstance(pending, dict) and pending.get("authorization_status") != "authorized":
            pending["authorization_status"] = "authorized"
            state.touch()
            self._write_checkpoint(state)
        return authorization_id

    def close_empty_pre_authorization_round(self, state: CycleState) -> None:
        evidence = self.validate_pre_authorization(state)
        pending = state.pending_round
        assert isinstance(pending, dict)
        state.pending_round = None
        self.save(
            state,
            event="round_preview_empty",
            round_number=evidence["round_number"],
            round_intent_id=evidence["round_intent_id"],
            selected_ids=[],
            authorization_id=None,
        )

    @staticmethod
    def _read_pre_authorization_archive(
        archive_path: Path,
        *,
        cycle_id: str,
        round_intent_id: str,
    ) -> dict[str, Any]:
        manifest_path = archive_path / "manifest.json"
        if archive_path.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
            raise RuntimeError("pre-authorization recovery archive is missing or unsafe")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("cycle_id") != cycle_id or manifest.get("round_intent_id") != round_intent_id:
            raise RuntimeError("pre-authorization archive identity conflict")
        files = manifest.get("files")
        if not isinstance(files, Mapping):
            raise RuntimeError("pre-authorization archive manifest is incomplete")
        for name in ("state.json", "journal.jsonl"):
            path = archive_path / name
            expected = files.get(name)
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"pre-authorization archive evidence changed: {name}")
        registry = files.get("media_registry.sqlite")
        if isinstance(registry, Mapping) and registry.get("status") == "archived":
            path = archive_path / "media_registry.sqlite"
            if (
                path.is_symlink()
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != registry.get("sha256")
            ):
                raise RuntimeError("pre-authorization archive evidence changed: media registry")
        return manifest

    def archive_pre_authorization(self, state: CycleState) -> dict[str, Any]:
        """Archive exact recovery evidence before resuming an interrupted preview."""
        evidence = self.validate_pre_authorization(state)
        cycle_id = re.sub(r"[^A-Za-z0-9_.-]", "_", state.cycle_id)[:80]
        intent_id = str(evidence["round_intent_id"])
        archive_root = self.directory / "recovery-audits"
        safe_intent_id = re.sub(r"[^A-Za-z0-9_.-]", "_", intent_id)[:128]
        archive_path = archive_root / f"{cycle_id}-{safe_intent_id}"
        manifest_path = archive_path / "manifest.json"
        if manifest_path.is_file():
            manifest = self._read_pre_authorization_archive(
                archive_path,
                cycle_id=state.cycle_id,
                round_intent_id=intent_id,
            )
            existing_events = self._events()
            if not any(
                event.get("event") == "round_preview_recovery_archived"
                and event.get("cycle_id") == state.cycle_id
                and event.get("round_intent_id") == intent_id
                for event in existing_events
            ):
                payload_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                self.save(
                    state,
                    event="round_preview_recovery_archived",
                    round_number=evidence["round_number"],
                    round_intent_id=intent_id,
                    archive_path=str(archive_path.relative_to(self.root)),
                    manifest_sha256=payload_hash,
                    selected_ids=None,
                    authorization_id=None,
                )
            return {"status": "already_archived", "path": str(archive_path)}

        archive_root.mkdir(parents=True, exist_ok=True)
        temporary = archive_root / f".{archive_path.name}.{uuid.uuid4().hex}.tmp"
        temporary.mkdir(mode=0o700)

        def write_private(path: Path, payload: bytes) -> str:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            return hashlib.sha256(payload).hexdigest()

        try:
            checkpoint_bytes = self.state_path.read_bytes()
            journal_bytes = self.journal_path.read_bytes()
            checkpoint_hash = write_private(temporary / "state.json", checkpoint_bytes)
            journal_hash = write_private(temporary / "journal.jsonl", journal_bytes)
            registry_source = self.root / "work" / "media_registry.sqlite"
            registry_status = "absent"
            registry_hash = None
            if registry_source.exists():
                if registry_source.is_symlink() or not registry_source.is_file():
                    raise RuntimeError("media registry path is not a regular file")
                registry_copy = temporary / "media_registry.sqlite"
                source_uri = f"file:{quote(str(registry_source.resolve()))}?mode=ro"
                with sqlite3.connect(source_uri, uri=True, timeout=5) as source_db:
                    with sqlite3.connect(registry_copy) as destination_db:
                        source_db.backup(destination_db)
                os.chmod(registry_copy, 0o600)
                with registry_copy.open("rb") as handle:
                    registry_hash = hashlib.sha256(handle.read()).hexdigest()
                registry_status = "archived"
            manifest = {
                "schema_version": 1,
                "status": "preauthorization_recovery_evidence",
                "cycle_id": state.cycle_id,
                "round_number": evidence["round_number"],
                "round_intent_id": intent_id,
                "created_at": _now(),
                "classification": "SAFE_TO_RESUME_PREVIEW",
                "writer_possible_by_protocol": False,
                "authorization_id": None,
                "selected_ids": None,
                "preview": {"status": "not_run_yet"},
                "files": {
                    "state.json": checkpoint_hash,
                    "journal.jsonl": journal_hash,
                    "media_registry.sqlite": {
                        "status": registry_status,
                        "sha256": registry_hash,
                    },
                },
            }
            manifest_payload = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8") + b"\n"
            write_private(temporary / "manifest.json", manifest_payload)
            os.replace(temporary, archive_path)
            dir_fd = os.open(archive_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            existing_events = self._events()
            if not any(
                event.get("event") == "round_preview_recovery_archived"
                and event.get("cycle_id") == state.cycle_id
                and event.get("round_intent_id") == intent_id
                for event in existing_events
            ):
                self.save(
                    state,
                    event="round_preview_recovery_archived",
                    round_number=evidence["round_number"],
                    round_intent_id=intent_id,
                    archive_path=str(archive_path.relative_to(self.root)),
                    manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
                    selected_ids=None,
                    authorization_id=None,
                )
            return {"status": "archived", "path": str(archive_path)}
        except Exception:
            if temporary.exists():
                for child in temporary.iterdir():
                    child.unlink(missing_ok=True)
                temporary.rmdir()
            raise

    def record_round_preview(
        self,
        state: CycleState,
        selected_ids: list[int] | tuple[int, ...],
        *,
        preview_sha256: str,
    ) -> None:
        evidence = self.validate_pre_authorization(state)
        normalized_ids = [int(value) for value in selected_ids]
        safe_intent_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(evidence["round_intent_id"]))[:128]
        archive_path = (
            self.directory
            / "recovery-audits"
            / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', state.cycle_id)[:80]}-{safe_intent_id}"
        )
        manifest_path = archive_path / "manifest.json"
        if manifest_path.is_file():
            manifest = self._read_pre_authorization_archive(
                archive_path,
                cycle_id=state.cycle_id,
                round_intent_id=str(evidence["round_intent_id"]),
            )
            manifest["preview"] = {
                "status": "completed",
                "selected_ids": normalized_ids,
                "sha256": preview_sha256,
                "recorded_at": _now(),
            }
            manifest["selected_ids"] = normalized_ids
            temporary = manifest_path.with_name(f".manifest.{uuid.uuid4().hex}.tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, manifest_path)
        self.save(
            state,
            event="round_preview_completed",
            round_number=evidence["round_number"],
            round_intent_id=evidence["round_intent_id"],
            selected_ids=normalized_ids,
            selection_sha256=preview_sha256,
            authorization_id=None,
        )

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
