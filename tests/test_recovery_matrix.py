"""Fixture matrix for the current recovery gates.

These fixtures intentionally contain synthetic identities only.  The matrix
records the first blocking gate instead of silently treating ambiguous state as
recoverable or replayable.
"""

import hashlib
import json

import pytest

from unicornio_editor.locking import classify_lock_owner_record
from unicornio_editor.media.registry import connect
from unicornio_editor.pipeline_v2.cycle_state import CycleStateStore
from unicornio_editor.pipeline_v2.journal_reader import (
    CycleJournalError,
    parse_cycle_journal_bytes,
)


def test_matrix_mixed_multiline_journal_preserves_bytes_and_hash():
    first = json.dumps(
        {"event": "cycle_started", "cycle_id": "fixture-cycle", "schema_version": 1},
        indent=2,
    )
    second = json.dumps(
        {"event": "round_started", "cycle_id": "fixture-cycle", "round_number": 1},
        separators=(",", ":"),
    )
    raw = (first + "\n" + second + "\n").encode("utf-8")

    journal = parse_cycle_journal_bytes(raw)

    assert journal.format == "json-pretty-or-stream"
    assert [item["event"] for item in journal.events] == ["cycle_started", "round_started"]
    assert journal.sha256 == hashlib.sha256(raw).hexdigest()


def test_matrix_truncated_journal_is_blocked_without_repair():
    raw = b'{"event":"cycle_started","cycle_id":"fixture-cycle"}\n{"event":"round_started"'

    with pytest.raises(CycleJournalError, match="invalid_or_truncated_json_event"):
        parse_cycle_journal_bytes(raw)


def test_matrix_pre_authorization_without_selection_is_blocked(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({"ready_target": 1}, cycle_id="checkpoint-fixture")
    store.start_round(state, round_number=1, request={"batch_size": 1})

    with pytest.raises(RuntimeError, match="authorized selection"):
        store.validate_round_authorization(state)

    assert state.pending_round["preview_selected_ids"] is None
    assert state.pending_round["authorization_id"] is None


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ({"released": False, "token": "legacy", "pid": 123}, "owner_host_missing_or_different"),
        ({"released": False, "token": "legacy", "pid": 123, "host": "current-host"}, "owner_process_identity_missing"),
        ({"released": False, "token": "legacy", "pid": 123, "host": "current-host", "process_start": ""}, "owner_process_identity_missing"),
    ],
)
def test_matrix_legacy_owner_without_independent_identity_stays_unverifiable(record, reason):
    status, detail = classify_lock_owner_record(record, host="current-host")

    assert status == "legacy_unverifiable"
    assert detail == reason


def test_matrix_registry_started_operation_has_no_completion_evidence(tmp_path):
    with connect(tmp_path) as db:
        db.execute(
            """INSERT INTO media_operations
               (post_id, run_id, cycle_id, cycle_round, authorization_id,
                phase, operation_status, started_at, completed_at,
                write_started, readback_confirmed, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                900001,
                "fixture-run",
                "fixture-cycle",
                1,
                "fixture-auth",
                "media",
                "started",
                "2026-11-10T00:00:00+00:00",
                None,
                0,
                0,
                "",
            ),
        )
        row = db.execute(
            "SELECT operation_status, completed_at, write_started, readback_confirmed FROM media_operations WHERE run_id=?",
            ("fixture-run",),
        ).fetchone()

    assert tuple(row) == ("started", None, 0, 0)
