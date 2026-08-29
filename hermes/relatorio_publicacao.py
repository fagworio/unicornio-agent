#!/usr/bin/env python3
"""Relatorio legivel da janela de publicacao (substitui o JSON cru).

Le o JSON do publish-ready (stdin ou work/publish-window.log), busca os
titulos dos posts e imprime um relatorio amigavel para o Telegram:
  - lista de posts (id, titulo, data de publicacao, status)
  - custo real de tokens (state.db do Hermes) da producao editorial nas
    ultimas 24h — e destaca quando a janela NAO publicou nada mas gastou.
Zero tokens de LLM (script-only).
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/www/wwwroot/hermes/unicornio-agent")
STATE_DB = Path("/root/.hermes/state.db")
LOG = ROOT / "work" / "publish-window.log"


def _load_window_json() -> dict | None:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip() and LOG.is_file():
        raw = LOG.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _post_titles(post_ids: list[int]) -> dict[int, str]:
    """Busca titulos via UMA unica chamada REST (include=) — rapido e sem
    multiplicar timeouts por post. Silencioso em falha (titulo vira o slug)."""
    titles: dict[int, str] = {}
    if not post_ids:
        return titles
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from unicornio_editor.config import load_config
        from unicornio_editor.wordpress import WordPressClient

        client = WordPressClient(load_config())
        resp = client._request(
            "GET", "/posts",
            {"include": ",".join(str(i) for i in post_ids),
             "per_page": 100, "_fields": "id,title"},
        )
        if isinstance(resp, list):
            for p in resp:
                if isinstance(p, dict) and isinstance(p.get("id"), int):
                    titles[p["id"]] = str((p.get("title") or {}).get("rendered", "")).strip()
    except Exception:
        pass
    return titles


def _custo_24h() -> tuple[float, int, str]:
    """Custo de cron nas últimas 24h, com atribuição por job quando possível.

    ``sessions.source='cron'`` pode incluir jobs sem relação com o editorial.
    Para atribuição exata, defina ``HERMES_EDITORIAL_CRON_JOB_ID`` com o ID do
    job editorial. Versões antigas do state.db sem coluna de job continuam
    funcionando, mas são identificadas explicitamente como custo agregado.
    """
    if not STATE_DB.is_file():
        return 0.0, 0, "state.db indisponível"
    try:
        db = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        job_id = os.environ.get("HERMES_EDITORIAL_CRON_JOB_ID", "").strip()
        job_column = next(
            (name for name in ("cron_job_id", "job_id") if name in columns),
            None,
        )
        query = (
            "SELECT COALESCE(SUM(estimated_cost_usd),0), COUNT(*) "
            "FROM sessions WHERE source='cron' "
            "AND started_at > strftime('%s','now') - 86400"
        )
        params: tuple[str, ...] = ()
        scope = "todos os crons"
        if job_id and job_column:
            query += f" AND {job_column} = ?"
            params = (job_id,)
            scope = f"job editorial {job_id}"
        elif job_id:
            scope = "todos os crons (state.db sem coluna de job)"
        row = db.execute(query, params).fetchone()
        db.close()
        return float(row[0] or 0), int(row[1] or 0), scope
    except sqlite3.Error:
        return 0.0, 0, "state.db inválido"


def _fmt_data(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone(timedelta(hours=-3))).strftime("%d/%m %H:%M")
    except ValueError:
        return iso[:16]


def main() -> int:
    data = _load_window_json()
    if data is None:
        print("publicacao: sem resultado da janela (nada publicado / relatorio indisponivel)")
        return 0

    published = data.get("posts") or []
    blocked = data.get("blocked_posts") or []
    custo, n_runs, cost_scope = _custo_24h()

    print("📰 Relatório de publicação")
    print(f"🕐 Janela: {datetime.now(timezone(timedelta(hours=-3))).strftime('%d/%m/%Y %H:%M')} (-03)")

    if published:
        ids = [p.get("post_id") for p in published if isinstance(p.get("post_id"), int)]
        titles = _post_titles(ids)
        print(f"\n✅ Publicados ({len(published)}):")
        for p in published:
            pid = p.get("post_id")
            link = p.get("link", "")
            slug = link.rstrip("/").split("/")[-1].replace("-", " ") if link else ""
            titulo = titles.get(pid) or slug or str(pid)
            print(f"  • {pid} | {titulo[:60]} | {_fmt_data(str(p.get('published_at','')))} | publish")
    else:
        print("\n✅ Publicados: 0")

    if blocked:
        ids = [b.get("post_id") for b in blocked if isinstance(b.get("post_id"), int)]
        titles = _post_titles(ids)
        print(f"\n⚠️ Bloqueados ({len(blocked)}):")
        for b in blocked:
            pid = b.get("post_id")
            fails = ", ".join(b.get("failed") or []) or b.get("reason", "")
            print(f"  • {pid} | {titles.get(pid, '')[:50]} | blocked | {fails[:60]}")
    else:
        print("⚠️ Bloqueados: 0")

    print(f"\n💰 Custo de cron (24h): ${custo:.3f} ({n_runs} runs; escopo: {cost_scope})")
    if published and n_runs:
        per = custo / len(published)
        print(f"   Custo por post publicado: ${per:.4f}")
    if not published and custo > 0:
        print(f"   ⚠️ Gasto sem publicação: ${custo:.3f} (nada saiu nesta janela)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
