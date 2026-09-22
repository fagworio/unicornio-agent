"""Probe read-only: content vs api_content, flags de mensagem (active/observed/compacted)."""
from __future__ import annotations

import pathlib
import sqlite3

STATE_DB = pathlib.Path.home() / ".hermes" / "state.db"
SESSAO = "cron_9e39343dc6f5_20260922_064523"


def main() -> None:
    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    print("resumo por role (sessao alvo):")
    for r in conn.execute(
        "SELECT role, COUNT(*), SUM(LENGTH(COALESCE(content,''))), "
        "SUM(LENGTH(COALESCE(api_content,''))), SUM(CASE WHEN api_content IS NULL THEN 1 ELSE 0 END) "
        "FROM messages WHERE session_id=? GROUP BY role", (SESSAO,)):
        print("  role=%s msgs=%s content_bytes=%s api_content_bytes=%s api_null=%s" % r)
    print("flags:")
    for r in conn.execute(
        "SELECT COALESCE(active,-1), COALESCE(observed,-1), COALESCE(compacted,-1), COUNT(*) "
        "FROM messages WHERE session_id=? GROUP BY 1,2,3", (SESSAO,)):
        print("  active/observed/compacted =", r)
    print("amostra de api_content != content:")
    for r in conn.execute(
        "SELECT id, role, LENGTH(COALESCE(content,'')), LENGTH(COALESCE(api_content,'')), "
        "SUBSTR(COALESCE(api_content,''),1,200) FROM messages WHERE session_id=? AND "
        "api_content IS NOT NULL AND api_content != content LIMIT 3", (SESSAO,)):
        print("  ", r[0], r[1], r[2], r[3], str(r[4])[:150].replace("\n", " | "))
    conn.close()


if __name__ == "__main__":
    main()
