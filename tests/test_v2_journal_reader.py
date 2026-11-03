import hashlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2.cycle_state import CycleState, CycleStateStore
from unicornio_editor.pipeline_v2.journal_reader import (
    CycleJournalError,
    parse_cycle_journal_bytes,
    read_cycle_journal,
)


def _append_from_process(root: str, writer_id: int, barrier) -> None:
    store = CycleStateStore(Path(root))
    barrier.wait(timeout=5)
    store._append_event(CycleState(cycle_id="multiprocess-cycle"), "writer_ping", writer_id=writer_id)


def _event(name="cycle_started", **extra):
    return {"event": name, "cycle_id": "cycle-1", **extra}


def test_reads_jsonl_and_historical_seven_line_pretty_event_without_rewrite(tmp_path):
    pretty = json.dumps(
        _event("cycle_started", note="ação 🦄", at="2026-11-03T10:00:00Z", schema_version=1),
        ensure_ascii=False,
        indent=2,
    )
    assert len(pretty.splitlines()) == 7
    later = json.dumps(_event("round_started", round_number=1))
    path = tmp_path / "journal.jsonl"
    path.write_bytes((pretty + "\n" + later + "\n").encode("utf-8"))
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    journal = read_cycle_journal(path)

    assert [row.event["event"] for row in journal.records] == ["cycle_started", "round_started"]
    assert journal.format == "json-pretty-or-stream"
    assert journal.records[0].start_line == 1
    assert journal.records[0].end_line == 7
    assert journal.records[1].start_line == 8
    assert journal.records[1].start_byte > journal.records[0].end_byte
    assert journal.sha256 == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_reads_multiple_pretty_objects_and_json_whitespace():
    raw = (
        " \t" + json.dumps(_event("cycle_started"), indent=2) + "\r\n\t"
        + json.dumps(_event("cycle_resumed"), indent=4) + "  \n"
    ).encode()
    journal = parse_cycle_journal_bytes(raw)
    assert len(journal.records) == 2
    assert journal.format == "json-pretty-or-stream"


def test_aggregate_journal_accepts_multiple_cycles_and_scopes_authorization_identity():
    raw = b"\n".join(
        json.dumps(
            {"event": "round_authorized", "cycle_id": cycle,
             "authorization_id": "shared-legacy-id", "round_number": round_number,
             "selected_ids": [post_id]},
            separators=(",", ":"),
        ).encode()
        for cycle, round_number, post_id in (("cycle-a", 1, 11), ("cycle-b", 4, 22))
    ) + b"\n"

    journal = parse_cycle_journal_bytes(raw)

    assert [event["cycle_id"] for event in journal.events] == ["cycle-a", "cycle-b"]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b'{"event":"cycle_started"}\n{"event":"cycle_started"}\n', "duplicate_event_record"),
        (b'{"event":"cycle_started"}\ntrailing garbage', "invalid_or_truncated_json_event"),
        (b'{"event":"cycle_started"}garbage', "event_separator_missing"),
        (b'{"event":"cycle_started"', "invalid_or_truncated_json_event"),
        (b'[]', "event_must_be_object"),
        (b'42', "event_must_be_object"),
        (b'{"cycle_id":"cycle-1"}', "event_name_missing"),
        (b'{"event":"round_authorized","cycle_id":"cycle-1","round_number":1,"authorization_id":"","selected_ids":[1]}', "invalid_authorization_id_identity"),
        (b'{"event":"round_authorized","cycle_id":"cycle-1","round_number":1,"authorization_id":"a","selected_ids":[1]}\n'
         b'{"event":"round_completed","cycle_id":"cycle-1","round_number":2,"authorization_id":"a"}', "authorization_identity_collision"),
        (b'{"event":"cycle_started"}\n\xff', "invalid_utf8"),
    ],
)
def test_rejects_corrupt_or_identity_divergent_journal(raw, reason):
    with pytest.raises(CycleJournalError) as error:
        parse_cycle_journal_bytes(raw)
    assert error.value.reason == reason
    assert error.value.line >= 1
    assert error.value.byte_offset >= 0


def test_store_read_is_read_only_and_append_adds_separator_to_pretty_legacy_journal(tmp_path):
    store = CycleStateStore(tmp_path)
    initial = json.dumps(_event(), ensure_ascii=False, indent=2).encode("utf-8")
    store.directory.mkdir(parents=True)
    store.journal_path.write_bytes(initial)
    before = hashlib.sha256(store.journal_path.read_bytes()).hexdigest()

    assert store._events()[0]["event"] == "cycle_started"
    assert hashlib.sha256(store.journal_path.read_bytes()).hexdigest() == before

    store._append_event(CycleState(cycle_id="cycle-1"), "cycle_resumed")
    journal = read_cycle_journal(store.journal_path)
    assert [row.event["event"] for row in journal.records] == ["cycle_started", "cycle_resumed"]
    assert store.journal_path.read_bytes().startswith(initial + b"\n")


def test_store_appends_new_cycle_without_rewriting_historical_events(tmp_path):
    store = CycleStateStore(tmp_path)
    store.create({}, cycle_id="cycle-a")
    store.create({}, cycle_id="cycle-b")

    journal = read_cycle_journal(store.journal_path)

    assert [event["cycle_id"] for event in journal.events] == ["cycle-a", "cycle-b"]


def test_invalid_journal_blocks_checkpoint_write(tmp_path):
    store = CycleStateStore(tmp_path)
    store.directory.mkdir(parents=True)
    store.state_path.write_bytes(b"original checkpoint")
    store.journal_path.write_bytes(b'{"event":"cycle_started"}\n{')
    state_before = store.state_path.read_bytes()
    journal_before = store.journal_path.read_bytes()

    with pytest.raises(RuntimeError, match="cannot append to invalid cycle journal"):
        store.save(CycleState(cycle_id="cycle-1"), event="cycle_resumed")

    assert store.state_path.read_bytes() == state_before
    assert store.journal_path.read_bytes() == journal_before


def test_conflicting_new_authorization_is_rejected_before_checkpoint_write(tmp_path):
    store = CycleStateStore(tmp_path)
    state = store.create({}, cycle_id="cycle-1")
    store._append_event(
        state,
        "round_authorized",
        round_number=1,
        authorization_id="auth-1",
        selected_ids=[101],
    )
    state_before = store.state_path.read_bytes()
    journal_before = store.journal_path.read_bytes()

    with pytest.raises(RuntimeError, match="new cycle event would invalidate journal"):
        store.save(
            state,
            event="round_authorized",
            round_number=1,
            authorization_id="auth-1",
            selected_ids=[102],
        )

    assert store.state_path.read_bytes() == state_before
    assert store.journal_path.read_bytes() == journal_before


def test_two_processes_append_without_interleaving(tmp_path):
    store = CycleStateStore(tmp_path)
    store.create({}, cycle_id="multiprocess-cycle")
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    processes = [
        context.Process(target=_append_from_process, args=(str(tmp_path), index, barrier))
        for index in (1, 2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
    assert all(process.exitcode == 0 for process in processes)
    rows = read_cycle_journal(store.journal_path).events
    assert [row["writer_id"] for row in rows if row["event"] == "writer_ping"] == [1, 2] or [
        row["writer_id"] for row in rows if row["event"] == "writer_ping"
    ] == [2, 1]


def test_fsync_failure_is_reported_and_does_not_create_malformed_event(tmp_path, monkeypatch):
    store = CycleStateStore(tmp_path)
    store.create({}, cycle_id="fsync-cycle")
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync failed")))

    with pytest.raises(OSError, match="fsync failed"):
        store._append_event(CycleState(cycle_id="fsync-cycle"), "cycle_resumed")

    assert [row["event"] for row in read_cycle_journal(store.journal_path).events] == [
        "cycle_started", "cycle_resumed"
    ]
