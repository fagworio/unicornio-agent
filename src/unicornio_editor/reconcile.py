"""Reconciliação de estado: WordPress × filesystem (P2.8).

O pipeline escreve em três lugares independentes — status/metas no WordPress,
artefatos em ``backups/<id>/`` (latest/blocked/draft/uncertain) e o manifesto
READY — sem transação distribuída. Quando uma etapa falha, os lados divergem em
silêncio:

    filesystem: BLOCKED      meta WP: AWAITING_HUMAN      status WP: pending

O resultado prático é retrabalho invisível: post que reaparece na fila, filtro
humano vazio, estado terminal sobrescrito por um novo apply.

Este módulo NÃO tenta transação distribuída. Ele lê os três lados, compara com
a máquina de estados esperada e reporta divergências com o reparo sugerido.
Somente leitura: nunca escreve no WordPress nem no disco.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .state import (
    META_ATTEMPTS,
    META_READY_HASH,
    STATE_AWAITING_HUMAN,
    STATE_BLOCKED,
    STATE_PUBLISHED,
    STATE_READY,
    STATE_SKIPPED,
    read_state,
)

# Artefatos de filesystem que carregam um veredicto do pipeline.
_ARTIFACTS = (
    "editorial.latest.json",
    "editorial.blocked.json",
    "editorial.draft.json",
    "uncertain.json",
)


def reconcile_post(post: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    """Divergências de UM post (status WP × meta × artefatos)."""
    post_id = int(post.get("id") or 0)
    meta = post.get("meta") or {}
    wp_status = str(post.get("status") or "")
    info = read_state(post)
    state = info.get("state")
    pasta = Path(root) / "backups" / str(post_id)
    tem = {nome: (pasta / nome).is_file() for nome in _ARTIFACTS}

    divergencias: list[dict[str, Any]] = []

    def add(code: str, detalhe: str, reparo: str) -> None:
        divergencias.append({
            "post_id": post_id,
            "code": code,
            "detail": detalhe,
            "suggested_repair": reparo,
            "wp_status": wp_status,
            "state": state,
        })

    # 1) Legado: post na fila sem nenhum marcador de estado.
    if state is None and wp_status in ("pending", "awaiting_human"):
        add("missing_state_marker",
            "post sem _hermes_state (formato legado) ainda na fila",
            "processar (prepare/apply) para gravar o estado; o reparo em massa "
            "exige uma operação explícita (o reconcile é somente leitura)")

    # 2) A experiência humana: o filtro do WP precisa refletir o estado.
    if state == STATE_AWAITING_HUMAN and wp_status != "awaiting_human":
        add("awaiting_human_status_mismatch",
            f"_hermes_state=awaiting_human mas status WP={wp_status!r} "
            "(o post não aparece no filtro Awaiting Human)",
            "mover o status WP para awaiting_human")
    if wp_status == "awaiting_human" and state not in (STATE_AWAITING_HUMAN, None):
        add("wp_awaiting_divergente",
            f"status WP=awaiting_human com _hermes_state={state!r}",
            "conferir decisão humana: retry (volta à fila) ou discard (encerra)")

    # 3) Artefatos terminais obsoletos (sobrevivem a um apply posterior).
    if tem["editorial.blocked.json"] and state in (STATE_READY, STATE_PUBLISHED):
        add("blocked_artifact_stale",
            "editorial.blocked.json presente com estado ready/published",
            "o apply bem-sucedido limpa o artefato; se persistir, reconferir o post")
    if tem["uncertain.json"] and state in (STATE_READY, STATE_PUBLISHED, STATE_SKIPPED):
        add("uncertain_artifact_stale",
            "uncertain.json presente com estado terminal diferente",
            "remover o artefato (o estado no WP é a fonte de verdade)")

    # 4) READY sem manifesto: o publish revalida, mas é divergência de contrato.
    if state == STATE_READY and not str(meta.get(META_READY_HASH) or "").strip():
        add("ready_sem_hash",
            "state=ready sem _hermes_ready_hash (manifesto ausente)",
            "re-rodar prepare/apply para emitir o manifesto do READY")

    # 5) Marcadores com tipo inválido.
    bruto = meta.get(META_ATTEMPTS)
    if bruto is not None:
        try:
            int(bruto)
        except (TypeError, ValueError):
            add("attempts_invalido", f"_hermes_attempts={bruto!r} não é inteiro",
                "corrigir o marcador para inteiro")

    return divergencias


def reconcile_state(
    client: Any,
    config: Any,
    root: Path,
    *,
    statuses: tuple[str, ...] = ("pending", "awaiting_human"),
    limit: int = 100,
) -> dict[str, Any]:
    """Varre os posts dos statuses dados e agrega as divergências.

    Somente leitura. `scanned` conta posts distintos analisados.
    """
    posts: list[dict[str, Any]] = []
    for status in statuses:
        try:
            posts.extend(client.list_pending(status=status, per_page=100) or [])
        except TypeError:  # cliente antigo sem o parâmetro status
            try:
                posts.extend(client.list_pending() or [])
            except Exception:  # noqa: BLE001 - reconciliação nunca derruba o run
                pass
        except Exception:  # noqa: BLE001
            continue

    vistos: set[int] = set()
    itens: list[dict[str, Any]] = []
    for post in posts:
        post_id = int(post.get("id") or 0)
        if not post_id or post_id in vistos:
            continue
        if len(vistos) >= limit:
            break
        vistos.add(post_id)
        itens.extend(reconcile_post(post, root))

    por_codigo = Counter(item["code"] for item in itens)
    return {
        "scanned": len(vistos),
        "divergences": len(itens),
        "by_code": dict(por_codigo),
        "items": itens,
        "read_only": True,
    }
