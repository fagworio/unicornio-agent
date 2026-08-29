#!/usr/bin/env python3
"""Freio diário de custo para o cron editorial.

É propositalmente opt-in: sem limite, sem ID do job ou sem uma coluna de job
no state.db, retorna ``allow``. Assim uma versão desconhecida do Hermes jamais
interrompe a publicação por engano. Quando configurado e o limite é atingido,
retorna ``block`` para o monitor manter uma saída estável e não acordar o LLM.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def cost_in_last_24h(state_db: Path, job_id: str) -> tuple[float, int] | None:
    """Retorna custo/runs do job, ou ``None`` se não houver atribuição segura."""
    if not state_db.is_file() or not job_id:
        return None
    try:
        db = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        job_column = next(
            (name for name in ("cron_job_id", "job_id") if name in columns),
            None,
        )
        if not job_column:
            db.close()
            return None
        row = db.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd),0), COUNT(*) "
            "FROM sessions WHERE source='cron' "
            f"AND {job_column} = ? "
            "AND started_at > strftime('%s','now') - 86400",
            (job_id,),
        ).fetchone()
        db.close()
        return float(row[0] or 0), int(row[1] or 0)
    except sqlite3.Error:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--job-id", default="")
    parser.add_argument("--limit", type=float, default=0.0)
    args = parser.parse_args()

    if args.limit <= 0:
        print(json.dumps({"decision": "allow", "reason": "limit_disabled"}))
        return 0
    measured = cost_in_last_24h(args.state_db, args.job_id.strip())
    if measured is None:
        print(json.dumps({"decision": "allow", "reason": "attribution_unavailable"}))
        return 0
    cost, runs = measured
    decision = "block" if cost >= args.limit else "allow"
    print(json.dumps({"decision": decision, "cost_usd": cost, "runs": runs, "limit_usd": args.limit}))
    return 10 if decision == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
