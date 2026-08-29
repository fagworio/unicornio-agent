#!/usr/bin/env python3
"""Relatorio legivel da janela de publicacao (substitui o JSON cru).

Le o JSON do publish-ready (stdin ou work/publish-window.log), busca os
titulos dos posts e imprime um relatorio amigavel para o Telegram:
  - lista de posts (id, titulo, data de publicacao, status)
  - custo real de tokens do cron editorial e publicacoes do mesmo fluxo nas
    mesmas ultimas 24h (nunca divide uma janela de publicacao pelo custo diário).
Zero tokens de LLM (script-only).
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("UNICORNIO_EDITOR_ROOT", "/www/wwwroot/hermes/unicornio-agent"))
STATE_DB = Path(os.environ.get("HERMES_STATE_DB", "/root/.hermes/state.db"))
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

    Prefere o ID exato do job editorial. Em bancos Hermes legados, sem essa
    coluna, filtra por ``cwd``/``git_repo_root`` do projeto — em vez de somar
    backup, watchdog e outros crons.
    """
    if not STATE_DB.is_file():
        return 0.0, 0, "state.db indisponível"
    try:
        try:
            # Import como pacote nos testes; como script no cron, o diretório
            # ``hermes/`` já está em sys.path.
            from hermes.cost_guard import cost_measurement_in_last_24h
        except ImportError:
            from cost_guard import cost_measurement_in_last_24h

        measured = cost_measurement_in_last_24h(
            STATE_DB,
            os.environ.get("HERMES_EDITORIAL_CRON_JOB_ID", "").strip(),
            str(ROOT),
        )
        if measured is None:
            return 0.0, 0, "atribuição editorial indisponível"
        return measured
    except (ImportError, sqlite3.Error):
        return 0.0, 0, "state.db inválido"


def _published_by_editorial_last_24h() -> int:
    """Count only durable publish events written by this pipeline in 24 hours."""
    path = ROOT / "work" / "telemetry.jsonl"
    if not path.is_file():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
            when = datetime.fromisoformat(str(record.get("ts", "")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if record.get("event") == "post_published" and when >= cutoff:
            count += 1
    return count


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

    print(f"\n💰 Custo editorial (24h): ${custo:.3f} ({n_runs} runs; escopo: {cost_scope})")
    published_24h = _published_by_editorial_last_24h()
    print(f"   Publicados pelo fluxo (24h): {published_24h}")
    if published_24h:
        print(f"   Custo por post publicado (mesma janela): ${custo / published_24h:.4f}")
    elif custo > 0:
        print("   ℹ️ Sem eventos de publicação editorial na telemetria das últimas 24h.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
