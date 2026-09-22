"""Ledger da sessao do agente: hard cap de posts tocados + budget de contexto.

O custo grande do pipeline nao vem de um gate frouxo: vem da SESSAO longa. Uma
execucao que persegue "5 READY" pode tocar 8, 10 ou 15 posts (``skipped``,
``uncertain`` e ``blocked`` nao consumiam a meta de READY) e o contexto
acumulado cresce a cada post — cada ``cards``, ``media-search-web``, ``draft``,
``content``, ``media-validate`` e ``apply`` empilha mais bytes na conversa, e
todo request seguinte relê tudo.

Este modulo mantem um ledger em ``work/session_state.json`` com:

* ``posts_touched`` — ids de posts em que a sessao JA gastou trabalho (um
  ``apply`` com desfecho real: ready/needs_rework/uncertain/skipped). E o HARD
  CAP: um post NOVO so entra enquanto restar vaga. Nenhum gate de qualidade
  muda — a sessao apenas encerra e o proximo post fica para a proxima janela.
* ``ready`` — quantos posts chegaram a READY nesta sessao (progresso contra
  ``EDITOR_TARGET_READY_PER_RUN``; a meta e distribuida entre sessoes curtas).
* ``context_bytes`` — soma dos bytes de stdout que os comandos de leitura
  devolveram ao LLM (medido por ``_record_cmd_output`` no CLI).

O ledger expira sozinho por INATIVIDADE (``EDITOR_SESSION_WINDOW_MINUTES``):
duas execucoes do cron sao sessoes diferentes, e o teto nunca pode bloquear a
proxima janela. Fail-soft por principio: qualquer erro de leitura/escrita
devolve "sem teto" — isto e orcamento, nunca gate de qualidade.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Config

_ESTADO_RELATIVO = Path("work") / "session_state.json"
_LOCK_RELATIVO = Path("work") / "session_state.lock"


def _caminho(root: Path | str) -> Path:
    return Path(root) / _ESTADO_RELATIVO


@contextlib.contextmanager
def _ledger_lock(root: Path | str) -> Iterator[None]:
    """Serializa leitura -> incremento -> escrita do ledger entre PROCESSOS.

    O cron editorial, o publish-ready (outro cron) e uma execucao manual podem
    tocar o mesmo diretorio: sem isto dois processos leem ``touched=1``, ambos
    gravam ``2`` e um terceiro post entra mesmo com o teto esgotado — exatamente
    o limite que o hard cap existe para impedir.

    ``flock`` e do SO (nao deixa lock orfao: o kernel libera quando o processo
    morre) e cobre tambem threads, porque cada ``open`` cria uma descricao de
    arquivo propria. Em plataforma sem ``fcntl`` o codigo segue sem lock — perder
    o lock e ruim, mas travar o pipeline por isso e pior (fail-soft).
    """
    caminho = Path(root) / _LOCK_RELATIVO
    handle = None
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        handle = open(caminho, "a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except Exception:  # noqa: BLE001 - lock e protecao, nunca gate
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
        handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass


def _agora(now: float | None = None) -> float:
    return float(now if now is not None else time.time())


def _ts(valor: Any) -> float:
    """Epoch da sessao gravada (aceita float/int; string ISO como fallback)."""
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, str) and valor:
        try:
            return datetime.fromisoformat(valor).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _vazio() -> dict[str, Any]:
    return {"started_at": "", "last_activity": "", "posts_touched": [], "ready": 0,
            "context_bytes": 0, "commands": 0}


def _normalizar(dados: Any) -> dict[str, Any]:
    if not isinstance(dados, dict):
        return _vazio()
    touched = [
        int(pid) for pid in (dados.get("posts_touched") or [])
        if isinstance(pid, (int, float)) or (isinstance(pid, str) and pid.isdigit())
    ]
    return {
        "started_at": str(dados.get("started_at") or ""),
        "last_activity": str(dados.get("last_activity") or ""),
        "posts_touched": touched,
        "ready": int(dados.get("ready") or 0),
        "context_bytes": int(dados.get("context_bytes") or 0),
        "commands": int(dados.get("commands") or 0),
    }


def load_session(root: Path | str, config: Config, *, now: float | None = None) -> dict[str, Any]:
    """Ledger atual da sessao (ja expirado pela janela de inatividade)."""
    try:
        dados = _normalizar(json.loads(_caminho(root).read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - ledger ausente/corrompido = sessao vazia
        return _vazio()
    janela = max(1, int(config.session_window_minutes)) * 60
    atividade = _ts(dados.get("last_activity") or dados.get("started_at"))
    if atividade and (_agora(now) - atividade) > janela:
        # Sessao anterior: o cron seguinte comeca com o teto zerado.
        return _vazio()
    return dados


def _salvar(root: Path | str, dados: dict[str, Any], *, now: float | None = None) -> None:
    caminho = _caminho(root)
    agora = _agora(now)
    dados = dict(dados)
    if not dados.get("started_at"):
        dados["started_at"] = datetime.fromtimestamp(agora, timezone.utc).isoformat(
            timespec="seconds"
        )
    dados["last_activity"] = datetime.fromtimestamp(agora, timezone.utc).isoformat(
        timespec="seconds"
    )
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        temporario = caminho.with_name(caminho.name + f".{os.getpid()}.tmp")
        temporario.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        os.replace(temporario, caminho)
    except Exception:  # noqa: BLE001 - orcamento nunca derruba o CLI
        pass


def cap_ativo(config: Config) -> bool:
    """Teto de posts tocados ligado? (``0`` desliga: papel do comportamento antigo.)"""
    return int(config.max_posts_touched_per_run) > 0


def status(
    root: Path | str, config: Config, *, now: float | None = None
) -> dict[str, Any]:
    """Projecao do orcamento da sessao (o que o agente precisa saber para parar)."""
    dados = load_session(root, config, now=now)
    tocados = dados["posts_touched"]
    teto = int(config.max_posts_touched_per_run)
    restantes = max(0, teto - len(tocados)) if cap_ativo(config) else None
    orcamento = int(config.session_context_bytes_budget)
    usado = int(dados["context_bytes"])
    return {
        "started_at": dados["started_at"],
        "target_ready": int(config.target_ready_per_run),
        "ready": int(dados["ready"]),
        "max_posts_touched": teto,
        "posts_touched": tocados,
        "posts_touched_count": len(tocados),
        "remaining_posts": restantes,
        "context_bytes_used": usado,
        "context_bytes_budget": orcamento,
        "context_budget_exceeded": bool(orcamento and usado >= orcamento),
    }


def touch_allowed(
    root: Path | str, post_id: int, config: Config, *, now: float | None = None
) -> tuple[bool, dict[str, Any]]:
    """Pode a sessao tocar este post agora?

    Um post JA tocado continua liberado (rework/correcao do mesmo post conta
    como o MESMO post tocado). Sem teto (0) ou sem ledger, libera.
    """
    projecao = status(root, config, now=now)
    if not cap_ativo(config):
        return True, projecao
    if int(post_id) in projecao["posts_touched"]:
        return True, projecao
    if projecao["remaining_posts"] and projecao["remaining_posts"] > 0:
        return True, projecao
    return False, projecao


def claim_touch(
    root: Path | str, post_id: int, config: Config, *, now: float | None = None
) -> tuple[bool, dict[str, Any]]:
    """Reserva a vaga da sessao de forma ATOMICA (checa E registra).

    ``touch_allowed`` seguido de ``record_touch`` tem uma janela entre a checagem
    e a gravacao: dois processos poderiam passar pelo teto ao mesmo tempo. Como o
    apply chama isto ANTES de escrever, a reserva e o proprio consumo da vaga —
    "post tocado" e o trabalho gasto nele, tenha o resultado sido READY ou nao.
    """
    with _ledger_lock(root):
        projecao = status(root, config, now=now)
        ja_tocado = int(post_id) in projecao["posts_touched"]
        if cap_ativo(config) and not ja_tocado and not projecao["remaining_posts"]:
            return False, projecao
        dados = load_session(root, config, now=now)
        if not ja_tocado:
            dados["posts_touched"].append(int(post_id))
            _salvar(root, dados, now=now)
        return True, status(root, config, now=now)


def record_ready(root: Path | str, config: Config, *, now: float | None = None) -> dict[str, Any]:
    """Conta um desfecho READY da sessao (progresso contra a meta)."""
    with _ledger_lock(root):
        dados = load_session(root, config, now=now)
        dados["ready"] = int(dados["ready"]) + 1
        _salvar(root, dados, now=now)
    return status(root, config, now=now)


def record_touch(
    root: Path | str, post_id: int, config: Config, *, ready: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Registra que a sessao gastou trabalho real neste post (com lock)."""
    with _ledger_lock(root):
        dados = load_session(root, config, now=now)
        if int(post_id) not in dados["posts_touched"]:
            dados["posts_touched"].append(int(post_id))
        if ready:
            dados["ready"] = int(dados["ready"]) + 1
        _salvar(root, dados, now=now)
    return status(root, config, now=now)


def record_context_bytes(
    root: Path | str, quantidade: int, config: Config, *, now: float | None = None
) -> dict[str, Any]:
    """Soma os bytes de stdout que o LLM acabou de consumir (com lock)."""
    with _ledger_lock(root):
        dados = load_session(root, config, now=now)
        dados["context_bytes"] = int(dados["context_bytes"]) + max(0, int(quantidade))
        dados["commands"] = int(dados["commands"]) + 1
        _salvar(root, dados, now=now)
    return status(root, config, now=now)


def stop_reason(root: Path | str, config: Config, *, now: float | None = None) -> str:
    """Motivo DETERMINISTICO para encerrar a sessao agora ("" = seguir).

    O agente nao decide: o codigo conta. Nunca simplifique o checklist para
    caber no orcamento — encerre a sessao.
    """
    projecao = status(root, config, now=now)
    if projecao["context_budget_exceeded"]:
        return (
            f"budget de contexto da sessao atingido "
            f"({projecao['context_bytes_used']} de {projecao['context_bytes_budget']} bytes); "
            "encerre a sessao sem novos comandos — o proximo post fica para a proxima janela"
        )
    if cap_ativo(config) and projecao["remaining_posts"] == 0:
        return (
            f"teto de posts tocados por sessao atingido "
            f"({projecao['posts_touched_count']}/{projecao['max_posts_touched']}); "
            "encerre a sessao — o proximo post fica para a proxima janela"
        )
    return ""


__all__ = [
    "cap_ativo",
    "claim_touch",
    "load_session",
    "record_context_bytes",
    "record_ready",
    "record_touch",
    "status",
    "stop_reason",
    "touch_allowed",
]
