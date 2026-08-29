#!/usr/bin/env python3
"""Freio diário de custo para o cron editorial.

É propositalmente opt-in. A atribuição prefere o ID exato do job e, em versões
do Hermes que ainda não persistem esse ID, usa o ``cwd``/``git_repo_root`` do
projeto. Sem uma dessas atribuições seguras retorna ``allow``. Quando
configurado e o limite é atingido, retorna ``block`` para o monitor manter uma
saída estável e não acordar o LLM.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _project_filter(columns: set[str], project_root: str) -> tuple[str, tuple[str, ...]] | None:
    """Return a safe per-project SQL predicate supported by this Hermes schema."""
    if not project_root:
        return None
    fields = [field for field in ("cwd", "git_repo_root") if field in columns]
    if not fields:
        return None
    predicate = " OR ".join(f"{field} = ?" for field in fields)
    return f"({predicate})", tuple(project_root for _ in fields)


def cost_measurement_in_last_24h(
    state_db: Path, job_id: str = "", project_root: str = ""
) -> tuple[float, int, str] | None:
    """Return ``(cost, runs, scope)`` with exact-job or project attribution."""
    if not state_db.is_file():
        return None
    try:
        db = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        job_column = next(
            (name for name in ("cron_job_id", "job_id") if name in columns),
            None,
        )
        where = ["source='cron'", "started_at > strftime('%s','now') - 86400"]
        params: tuple[str, ...] = ()
        if job_id and job_column:
            where.append(f"{job_column} = ?")
            params = (job_id,)
            scope = f"job editorial {job_id}"
        else:
            project_filter = _project_filter(columns, project_root)
            if project_filter is None:
                db.close()
                return None
            predicate, project_params = project_filter
            where.append(predicate)
            params = project_params
            scope = "projeto editorial (cwd/git_repo_root)"
        row = db.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd),0), COUNT(*) FROM sessions WHERE "
            + " AND ".join(where), params,
        ).fetchone()
        db.close()
        return float(row[0] or 0), int(row[1] or 0), scope
    except sqlite3.Error:
        return None


def cost_in_last_24h(
    state_db: Path, job_id: str = "", project_root: str = ""
) -> tuple[float, int] | None:
    """Backward-compatible cost/runs accessor used by scripts and tests."""
    measured = cost_measurement_in_last_24h(state_db, job_id, project_root)
    return None if measured is None else measured[:2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--job-id", default="")
    parser.add_argument("--project-root", default="")
    parser.add_argument("--limit", type=float, default=0.0)
    args = parser.parse_args()

    if args.limit <= 0:
        print(json.dumps({"decision": "allow", "reason": "limit_disabled"}))
        return 0
    measured = cost_measurement_in_last_24h(
        args.state_db, args.job_id.strip(), args.project_root.strip()
    )
    if measured is None:
        print(json.dumps({"decision": "allow", "reason": "attribution_unavailable"}))
        return 0
    cost, runs, scope = measured
    decision = "block" if cost >= args.limit else "allow"
    print(json.dumps({"decision": decision, "cost_usd": cost, "runs": runs, "limit_usd": args.limit, "scope": scope}))
    return 10 if decision == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
