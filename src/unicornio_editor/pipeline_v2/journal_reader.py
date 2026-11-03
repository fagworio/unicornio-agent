"""Strict, read-only reader for the V2 cycle event journal.

The writer emits JSONL, while a small number of historical journals contain
pretty-printed JSON objects.  This reader accepts either representation (or a
sequence of whitespace-delimited objects) without rewriting the source.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_CYCLE_JOURNAL_BYTES = 32 * 1024 * 1024
MAX_CYCLE_JOURNAL_EVENTS = 200_000
_JSON_WHITESPACE = " \t\r\n"


class CycleJournalError(ValueError):
    """A cycle journal is incomplete, malformed, or internally inconsistent."""

    def __init__(self, reason: str, *, line: int = 1, byte_offset: int = 0):
        self.reason = reason
        self.line = line
        self.byte_offset = byte_offset
        super().__init__(f"{reason} at line {line}, byte {byte_offset}")


@dataclass(frozen=True)
class CycleJournalRecord:
    event: dict[str, Any]
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class CycleJournal:
    records: tuple[CycleJournalRecord, ...]
    sha256: str
    format: str

    @property
    def events(self) -> list[dict[str, Any]]:
        return [record.event for record in self.records]


def _identity_value(event: dict[str, Any], field: str, *, line: int, offset: int) -> Any:
    value = event.get(field)
    if value is None:
        return None
    if field == "round_number":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise CycleJournalError("invalid_round_identity", line=line, byte_offset=offset)
        return value
    if field == "selected_ids":
        if not isinstance(value, list) or any(
            isinstance(post_id, bool) or not isinstance(post_id, int) or post_id < 1
            for post_id in value
        ) or len(value) != len(set(value)):
            raise CycleJournalError("invalid_selected_ids_identity", line=line, byte_offset=offset)
        return tuple(value)
    if not isinstance(value, str) or not value.strip():
        raise CycleJournalError(f"invalid_{field}_identity", line=line, byte_offset=offset)
    return value.strip()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def parse_cycle_journal_bytes(raw: bytes) -> CycleJournal:
    """Parse a bounded sequence of JSON event objects without modifying bytes."""
    if not isinstance(raw, bytes):
        raise TypeError("cycle journal input must be bytes")
    if len(raw) > MAX_CYCLE_JOURNAL_BYTES:
        raise CycleJournalError("journal_size_limit_exceeded")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CycleJournalError(
            "invalid_utf8",
            line=raw[:exc.start].count(b"\n") + 1,
            byte_offset=exc.start,
        ) from exc
    if text.startswith("\ufeff"):
        raise CycleJournalError("utf8_bom_not_allowed")

    decoder = json.JSONDecoder(
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
    )
    records: list[CycleJournalRecord] = []
    canonical_records: set[bytes] = set()
    # The aggregate journal is append-only across cycles. Keep authorization
    # provenance scoped by its composite identity instead of assuming one
    # cycle per file.
    authorization_identity: dict[tuple[str, str], dict[str, Any]] = {}
    pos = 0
    byte_pos = 0
    line = 1

    while True:
        while pos < len(text) and text[pos] in _JSON_WHITESPACE:
            char = text[pos]
            byte_pos += len(char.encode("utf-8"))
            if char == "\n":
                line += 1
            pos += 1
        if pos == len(text):
            break
        start = pos
        start_byte = byte_pos
        start_line = line
        try:
            value, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError as exc:
            error_line = line + text[pos:exc.pos].count("\n")
            offset = start_byte + len(text[start:exc.pos].encode("utf-8"))
            raise CycleJournalError("invalid_or_truncated_json_event", line=error_line, byte_offset=offset) from exc
        except ValueError as exc:
            reason = "duplicate_json_object_key" if "duplicate JSON" in str(exc) else "non_standard_json_value"
            raise CycleJournalError(reason, line=start_line, byte_offset=start_byte) from exc
        if not isinstance(value, dict):
            raise CycleJournalError("event_must_be_object", line=start_line, byte_offset=start_byte)
        if not isinstance(value.get("event"), str) or not value["event"].strip():
            raise CycleJournalError("event_name_missing", line=start_line, byte_offset=start_byte)

        encoded_event = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if encoded_event in canonical_records:
            raise CycleJournalError("duplicate_event_record", line=start_line, byte_offset=start_byte)
        canonical_records.add(encoded_event)

        event_cycle = _identity_value(value, "cycle_id", line=start_line, offset=start_byte)

        round_number = _identity_value(value, "round_number", line=start_line, offset=start_byte)
        authorization_id = _identity_value(value, "authorization_id", line=start_line, offset=start_byte)
        selected_ids = _identity_value(value, "selected_ids", line=start_line, offset=start_byte)
        if authorization_id is not None:
            identity = authorization_identity.setdefault((event_cycle or "", authorization_id), {})
            for key, current in (
                ("cycle_id", event_cycle),
                ("round_number", round_number),
                ("selected_ids", selected_ids),
            ):
                if current is None:
                    continue
                previous = identity.get(key)
                if previous is not None and previous != current:
                    raise CycleJournalError("authorization_identity_collision", line=start_line, byte_offset=start_byte)
                identity[key] = current

        encoded_segment = text[start:end]
        segment_bytes = len(encoded_segment.encode("utf-8"))
        end_byte = start_byte + segment_bytes
        end_line = start_line + encoded_segment.count("\n")
        records.append(CycleJournalRecord(value, start_byte, end_byte, start_line, end_line))
        if len(records) > MAX_CYCLE_JOURNAL_EVENTS:
            raise CycleJournalError("journal_event_limit_exceeded", line=start_line, byte_offset=start_byte)
        pos = end
        byte_pos = end_byte
        line = end_line
        if pos < len(text) and text[pos] not in _JSON_WHITESPACE:
            raise CycleJournalError("event_separator_missing", line=line, byte_offset=byte_pos)

    multiline = any(record.start_line != record.end_line for record in records)
    return CycleJournal(
        records=tuple(records),
        sha256=hashlib.sha256(raw).hexdigest(),
        format="json-pretty-or-stream" if multiline else "jsonl",
    )


def read_cycle_journal(path: Path) -> CycleJournal:
    """Read and validate a cycle journal, rejecting symlinks and oversized data."""
    path = Path(path)
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CycleJournalError("journal_not_regular_file")
        if info.st_size > MAX_CYCLE_JOURNAL_BYTES:
            raise CycleJournalError("journal_size_limit_exceeded")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            current = path.stat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise CycleJournalError("journal_replaced_during_read")
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_CYCLE_JOURNAL_BYTES:
                raise CycleJournalError("journal_not_regular_or_oversized")
            chunks: list[bytes] = []
            remaining = MAX_CYCLE_JOURNAL_BYTES + 1
            while remaining > 0:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > MAX_CYCLE_JOURNAL_BYTES:
                raise CycleJournalError("journal_size_limit_exceeded")
            final = os.fstat(fd)
            final_path = path.stat()
            if (
                (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
                or (final.st_dev, final.st_ino) != (final_path.st_dev, final_path.st_ino)
                or len(raw) != final.st_size
            ):
                raise CycleJournalError("journal_changed_during_read")
        finally:
            os.close(fd)
    except CycleJournalError:
        raise
    except OSError as exc:
        raise CycleJournalError("journal_read_failed") from exc
    return parse_cycle_journal_bytes(raw)
