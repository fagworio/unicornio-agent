"""Probe read-only: esquema do telemetry.jsonl e do state.db (nenhuma escrita)."""
from __future__ import annotations

import collections
import json
import pathlib
import sqlite3
import sys

PROD = pathlib.Path("/www/wwwroot/hermes/unicornio-agent")
TELEMETRY = PROD / "work" / "telemetry.jsonl"
STATE_DB = pathlib.Path.home() / ".hermes" / "state.db"


def probe_telemetry() -> None:
    print("== TELEMETRY:", TELEMETRY)
    ev: collections.Counter = collections.Counter()
    keys: collections.Counter = collections.Counter()
    samples: dict = {}
    with TELEMETRY.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ev[rec.get("event")] += 1
            for k in rec:
                keys[k] += 1
            samples.setdefault(rec.get("event"), rec)
    print("eventos:")
    for name, n in ev.most_common(40):
        print(f"  {name}: {n}")
    print("campos:")
    for name, n in keys.most_common(30):
        print(f"  {name}: {n}")
    print("amostras:")
    for name in ("cmd_context", "cmd_output", "tool_context", "post_started",
                 "apply_ready", "apply_blocked", "media_search_result", "session_start"):
        if name in samples:
            print(f"  {name}: {json.dumps(samples[name], ensure_ascii=False)[:500]}")


def probe_state(session_prefix: str = "cron_9e39343dc6f5_") -> None:
    print("\n== STATE.DB:", STATE_DB)
    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT id, api_call_count, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, tool_call_count, estimated_cost_usd, "
        "message_count, ended_at, started_at FROM sessions WHERE id LIKE ? "
        "ORDER BY started_at DESC LIMIT 3",
        (session_prefix + "%",),
    ).fetchall()
    for r in row:
        print(" sessao:", r)
    sid = row[0][0]
    print("\n-- mensagens da sessao", sid)
    for r in conn.execute(
        "SELECT role, COUNT(*), SUM(COALESCE(token_count,0)), SUM(LENGTH(COALESCE(content,''))) "
        "FROM messages WHERE session_id=? GROUP BY role", (sid,)):
        print("   role=%s msgs=%s tokens=%s bytes_content=%s" % r)
    print("   compacted:", conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=? AND compacted=1", (sid,)).fetchone())
    print("   tool msgs por tool_name:", conn.execute(
        "SELECT tool_name, COUNT(*), SUM(LENGTH(COALESCE(content,''))) FROM messages "
        "WHERE session_id=? AND role='tool' GROUP BY tool_name ORDER BY 3 DESC LIMIT 10",
        (sid,)).fetchall())
    print("\n   session_model_usage:", conn.execute(
        "SELECT task, api_call_count, input_tokens, cache_read_tokens, cache_write_tokens, "
        "output_tokens, estimated_cost_usd FROM session_model_usage WHERE session_id=?",
        (sid,)).fetchall())
    conn.close()


if __name__ == "__main__":
    probe_telemetry()
    probe_state(sys.argv[1] if len(sys.argv) > 1 else "cron_9e39343dc6f5_")
