"""Ledger da sessao do agente: hard cap de posts tocados + budget de contexto.

O custo grande do pipeline nao vem de um gate frouxo: vem da SESSAO longa. Uma
execucao que persegue "5 READY" pode tocar 8, 10 ou 15 posts (``skipped``,
``uncertain`` e ``blocked`` nao consumiam a meta de READY) e o contexto
acumulado cresce a cada post — cada ``cards``, ``media-search-web``, ``draft``,
``content``, ``media-validate`` e ``apply`` empilha mais bytes na conversa, e
todo request seguinte relê tudo.

Este modulo mantem um ledger por sessão em ``work/sessions/<session_id>.json``
quando ``HERMES_SESSION_ID`` está disponível. Sem esse id, mantém o ledger
compatível em ``work/session_state.json``. Cada ledger contém:

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
proxima janela. Falhas de leitura, lock, escrita ou readback falham fechado:
nenhum trabalho novo pode começar com um orçamento que não foi comprovado.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Config

_ESTADO_RELATIVO = Path("work") / "session_state.json"
_LOCK_RELATIVO = Path("work") / "session_state.lock"
_LEDGER_LOCK_TIMEOUT_SECONDS = 2.0
_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _record_budget_telemetry(root: Path | str, event: str, **fields: Any) -> None:
    try:
        from .observability import append_telemetry

        append_telemetry(root, event, **fields)
    except Exception:  # noqa: BLE001 - telemetry must not affect budget safety
        pass


class SessionBudgetError(RuntimeError):
    """The session ledger cannot be trusted or safely persisted."""

    error_code = "session_budget_error"


class LedgerCorruptError(SessionBudgetError):
    """The persisted session ledger is malformed."""

    error_code = "ledger_corrupt"


class LedgerLockTimeout(SessionBudgetError):
    """The session ledger lock stayed busy past its bounded wait."""

    error_code = "ledger_lock_timeout"

    def __init__(self, message: str, *, wait_ms: float | None = None):
        super().__init__(message)
        self.wait_ms = wait_ms


class LedgerLockError(SessionBudgetError):
    """The session ledger lock could not be safely acquired or released."""

    error_code = "ledger_lock_error"


class LedgerReadError(SessionBudgetError):
    """The session ledger could not be read."""

    error_code = "ledger_read_error"


class LedgerPersistenceError(SessionBudgetError):
    """The session ledger could not be durably saved and verified."""

    error_code = "ledger_persistence_error"


def _session_id() -> str:
    """Retorna o id do Hermes sem permitir escapar de ``work/sessions``."""
    return str(os.environ.get("HERMES_SESSION_ID") or "").strip()


def current_owner_id() -> str:
    """Process-scoped reservation owner, fenced against PID reuse."""
    from .locking import _process_start_identity

    pid = os.getpid()
    start_identity = _process_start_identity(pid) or "unknown"
    host_identity = hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:12]
    return f"process:{host_identity}:{pid}:{start_identity}"


def _reservation_owner_record(owner_id: str) -> dict[str, Any]:
    owner = _validate_owner_id(owner_id)
    if owner.startswith("session:"):
        return {"owner_id": owner, "owner_kind": "session"}
    process_parts = owner.split(":", 3)
    if (
        len(process_parts) == 4
        and process_parts[0] == "process"
        and process_parts[1]
        == hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:12]
        and process_parts[2].isdigit()
        and int(process_parts[2]) == os.getpid()
    ):
        return {
            "owner_id": owner,
            "owner_kind": "process",
            "owner_host": socket.gethostname(),
            "owner_pid": os.getpid(),
            "owner_start_identity": process_parts[3],
        }
    return {"owner_id": owner, "owner_kind": "explicit"}


def _reservation_owner_recovery_proof(record: dict[str, Any]) -> str | None:
    """Return a proof code only when local process identity proves the owner is gone."""
    if record.get("owner_kind") != "process":
        return None
    if record.get("owner_host") != socket.gethostname():
        return None
    pid = record.get("owner_pid")
    start_identity = record.get("owner_start_identity")
    if (
        type(pid) is not int
        or pid < 1
        or not isinstance(start_identity, str)
    ):
        return None
    from .locking import _process_start_identity

    if start_identity and start_identity != "unknown":
        current_identity = _process_start_identity(pid)
        if current_identity is not None:
            return "owner_pid_reused" if current_identity != start_identity else None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "owner_process_exited"
    except (PermissionError, OSError):
        return None
    return None


def _session_slug(session_id: str) -> str:
    slug = _SESSION_ID_RE.sub("-", session_id).strip("-.")
    return slug[:160] or "anonymous"


def _caminho(root: Path | str) -> Path:
    session_id = _session_id()
    if session_id:
        return Path(root) / "work" / "sessions" / f"{_session_slug(session_id)}.json"
    # Compatibilidade para execuções locais/manuals sem um Hermes session id.
    return Path(root) / _ESTADO_RELATIVO


def _lock_path(root: Path | str) -> Path:
    session_id = _session_id()
    if session_id:
        return Path(root) / "work" / "sessions" / f"{_session_slug(session_id)}.lock"
    return Path(root) / _LOCK_RELATIVO


@contextlib.contextmanager
def _ledger_lock(
    root: Path | str,
    *,
    timeout_seconds: float = _LEDGER_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serializa leitura -> incremento -> escrita do ledger entre PROCESSOS.

    O cron editorial, o publish-ready (outro cron) e uma execucao manual podem
    tocar o mesmo diretorio: sem isto dois processos leem ``touched=1``, ambos
    gravam ``2`` e um terceiro post entra mesmo com o teto esgotado — exatamente
    o limite que o hard cap existe para impedir.

    ``flock`` e do SO (nao deixa lock orfao: o kernel libera quando o processo
    morrer) e cobre tambem threads, porque cada ``open`` cria uma descricao de
    arquivo propria. A espera e limitada; falha ao adquirir o lock bloqueia
    qualquer novo trabalho em vez de continuar sem protecao.
    """
    lock_started_at = time.monotonic()
    caminho = _lock_path(root)
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        handle = open(caminho, "a+", encoding="utf-8")
    except OSError as exc:
        _record_budget_telemetry(
            root,
            "session_budget_lock",
            outcome="open_failed",
            wait_ms=round((time.monotonic() - lock_started_at) * 1000, 1),
        )
        raise LedgerLockError("session budget lock could not be opened") from exc
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                wait_ms = round((time.monotonic() - lock_started_at) * 1000, 1)
                handle.close()
                _record_budget_telemetry(
                    root,
                    "session_budget_lock",
                    outcome="timeout",
                    wait_ms=wait_ms,
                )
                raise LedgerLockTimeout(
                    "session budget ledger lock timed out", wait_ms=wait_ms
                ) from exc
            time.sleep(min(0.05, remaining))
        except OSError as exc:
            wait_ms = round((time.monotonic() - lock_started_at) * 1000, 1)
            handle.close()
            _record_budget_telemetry(
                root,
                "session_budget_lock",
                outcome="failed",
                wait_ms=wait_ms,
            )
            raise LedgerLockError("session budget ledger lock failed") from exc
    acquired_at = time.monotonic()
    wait_ms = round((acquired_at - lock_started_at) * 1000, 1)

    def release() -> None:
        release_error: OSError | None = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            release_error = exc
        try:
            handle.close()
        except OSError as exc:
            release_error = release_error or exc
        _record_budget_telemetry(
            root,
            "session_budget_lock",
            outcome="release_failed" if release_error else "released",
            wait_ms=wait_ms,
            hold_ms=round((time.monotonic() - acquired_at) * 1000, 1),
        )
        if release_error is not None:
            raise LedgerLockError("session budget ledger lock could not be released") from release_error

    try:
        yield
    except BaseException as operation_error:
        try:
            release()
        except SessionBudgetError as release_error:
            if hasattr(operation_error, "add_note"):
                operation_error.add_note(str(release_error))
        raise
    else:
        release()


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
    return {
        "session_id": _session_id(),
        "started_at": "",
        "last_activity": "",
        "posts_touched": [],
        "posts_reserved": [],
        "reservation_owners": {},
        "reservation_recovery_history": [],
        "ready": 0,
        "context_bytes": 0,
        "commands": 0,
    }


def _normalizar(dados: Any) -> dict[str, Any]:
    if not isinstance(dados, dict):
        raise LedgerCorruptError("session budget ledger must be a JSON object")
    touched = [
        int(pid) for pid in (dados.get("posts_touched") or [])
        if isinstance(pid, (int, float)) or (isinstance(pid, str) and pid.isdigit())
    ]
    reserved = [
        int(pid) for pid in (dados.get("posts_reserved") or [])
        if isinstance(pid, (int, float)) or (isinstance(pid, str) and pid.isdigit())
    ]
    raw_owners = dados.get("reservation_owners", {})
    if not isinstance(raw_owners, dict):
        raise LedgerCorruptError("session budget reservation owners must be an object")
    reservation_owners: dict[str, dict[str, Any]] = {}
    for raw_post_id, record in raw_owners.items():
        if not isinstance(raw_post_id, str) or not raw_post_id.isdigit() or not isinstance(record, dict):
            raise LedgerCorruptError("session budget reservation owner record is malformed")
        post_id = int(raw_post_id)
        owner_id = record.get("owner_id")
        if post_id not in reserved or not isinstance(owner_id, str) or not owner_id.strip():
            raise LedgerCorruptError("session budget reservation owner does not match a reservation")
        owner_kind = record.get("owner_kind", "explicit")
        if owner_kind not in {"session", "process", "explicit", "legacy"}:
            raise LedgerCorruptError("session budget reservation owner kind is invalid")
        normalized_owner: dict[str, Any] = {
            "owner_id": owner_id.strip(),
            "owner_kind": owner_kind,
        }
        if owner_kind == "process":
            pid = record.get("owner_pid")
            host = record.get("owner_host")
            start_identity = record.get("owner_start_identity")
            if type(pid) is not int or pid < 1 or not isinstance(host, str) or not isinstance(start_identity, str):
                raise LedgerCorruptError("session budget process owner identity is malformed")
            owner_parts = owner_id.strip().split(":", 3)
            if len(owner_parts) == 4:
                identity_matches = (
                    owner_parts[0] == "process"
                    and owner_parts[1]
                    == hashlib.sha256(host.encode("utf-8")).hexdigest()[:12]
                    and owner_parts[2] == str(pid)
                    and owner_parts[3] == start_identity
                )
            else:
                # Read pre-host-fence ledgers but still verify their PID/start tuple.
                identity_matches = (
                    len(owner_parts) == 3
                    and owner_parts[0] == "process"
                    and owner_parts[1] == str(pid)
                    and owner_parts[2] == start_identity
                )
            if not identity_matches:
                raise LedgerCorruptError("session budget process owner identity does not match owner id")
            normalized_owner.update({
                "owner_pid": pid,
                "owner_host": host,
                "owner_start_identity": start_identity,
            })
        reservation_owners[str(post_id)] = normalized_owner
    recovery_history = dados.get("reservation_recovery_history", [])
    if not isinstance(recovery_history, list) or any(not isinstance(row, dict) for row in recovery_history):
        raise LedgerCorruptError("session budget reservation recovery history is malformed")
    return {
        "session_id": str(dados.get("session_id") or _session_id()),
        "started_at": str(dados.get("started_at") or ""),
        "last_activity": str(dados.get("last_activity") or ""),
        "posts_touched": touched,
        "posts_reserved": reserved,
        "reservation_owners": reservation_owners,
        "reservation_recovery_history": recovery_history[-100:],
        "ready": int(dados.get("ready") or 0),
        "context_bytes": int(dados.get("context_bytes") or 0),
        "commands": int(dados.get("commands") or 0),
    }


def load_session(root: Path | str, config: Config, *, now: float | None = None) -> dict[str, Any]:
    """Ledger atual da sessao (ja expirado pela janela de inatividade)."""
    read_started_at = time.monotonic()

    def _record_read(outcome: str, byte_count: int = 0) -> None:
        _record_budget_telemetry(
            root,
            "session_budget_ledger_read",
            outcome=outcome,
            duration_ms=round((time.monotonic() - read_started_at) * 1000, 1),
            byte_count=byte_count,
        )

    try:
        raw = _caminho(root).read_text(encoding="utf-8")
    except FileNotFoundError:
        _record_read("missing")
        return _vazio()
    except OSError as exc:
        _record_read("read_failed")
        raise LedgerReadError("session budget ledger could not be read") from exc
    try:
        dados = _normalizar(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError, LedgerCorruptError) as exc:
        _record_read("corrupt", len(raw.encode("utf-8")))
        if isinstance(exc, LedgerCorruptError):
            raise
        raise LedgerCorruptError("session budget ledger is malformed") from exc
    janela = max(1, int(config.session_window_minutes)) * 60
    atividade = _ts(dados.get("last_activity") or dados.get("started_at"))
    if (
        atividade
        and (_agora(now) - atividade) > janela
        and not dados["posts_reserved"]
    ):
        # Sessao anterior: o cron seguinte comeca com o teto zerado.
        _record_read("expired", len(raw.encode("utf-8")))
        return _vazio()
    _record_read("loaded", len(raw.encode("utf-8")))
    return dados


def _salvar(root: Path | str, dados: dict[str, Any], *, now: float | None = None) -> None:
    caminho = _caminho(root)
    agora = _agora(now)
    dados = dict(dados)
    dados["session_id"] = _session_id()
    if not dados.get("started_at"):
        dados["started_at"] = datetime.fromtimestamp(agora, timezone.utc).isoformat(
            timespec="seconds"
        )
    dados["last_activity"] = datetime.fromtimestamp(agora, timezone.utc).isoformat(
        timespec="seconds"
    )
    temporario: Path | None = None
    fd = -1
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{caminho.name}.", dir=caminho.parent)
        temporario = Path(temp_name)
        os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream:
            stream.write(json.dumps(dados, ensure_ascii=False))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporario, caminho)
        directory_fd = os.open(
            caminho.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        persisted = json.loads(caminho.read_text(encoding="utf-8"))
        if persisted != dados:
            raise LedgerCorruptError("session budget ledger readback mismatch")
    except Exception as exc:  # noqa: BLE001 - budget errors must block new work
        try:
            if fd >= 0:
                os.close(fd)
            if temporario is not None:
                temporario.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, SessionBudgetError):
            raise
        raise LedgerPersistenceError("session budget ledger could not be saved") from exc


def cap_ativo(config: Config) -> bool:
    """Teto de posts tocados ligado? (``0`` desliga: papel do comportamento antigo.)"""
    return int(config.max_posts_touched_per_run) > 0


def _validate_owner_id(owner_id: str) -> str:
    if not isinstance(owner_id, str):
        raise SessionBudgetError("reservation owner_id must be a string")
    owner = owner_id.strip()
    if not owner or len(owner) > 256:
        raise SessionBudgetError("reservation owner_id is empty or too long")
    return owner


def _projection(dados: dict[str, Any], config: Config) -> dict[str, Any]:
    tocados = list(dados["posts_touched"])
    reservados = list(dados["posts_reserved"])
    reservados_de_capacidade = [pid for pid in reservados if pid not in tocados]
    teto = int(config.max_posts_touched_per_run)
    restantes = (
        max(0, teto - len(tocados) - len(reservados_de_capacidade))
        if cap_ativo(config)
        else None
    )
    orcamento = int(config.session_context_bytes_budget)
    usado = int(dados["context_bytes"])
    return {
        "session_id": str(dados.get("session_id") or _session_id()),
        "started_at": dados["started_at"],
        "target_ready": int(config.target_ready_per_run),
        "ready": int(dados["ready"]),
        "max_posts_touched": teto,
        "posts_touched": tocados,
        "posts_touched_count": len(tocados),
        "posts_reserved": reservados,
        "posts_reserved_count": len(reservados),
        "posts_reserved_capacity_count": len(reservados_de_capacidade),
        "remaining_posts": restantes,
        "context_bytes_used": usado,
        "context_bytes_budget": orcamento,
        "context_budget_exceeded": bool(orcamento and usado >= orcamento),
    }


def status(
    root: Path | str, config: Config, *, now: float | None = None
) -> dict[str, Any]:
    """Projecao do orcamento da sessao (o que o agente precisa saber para parar)."""
    return _projection(load_session(root, config, now=now), config)


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
    if int(post_id) in projecao["posts_touched"] or int(post_id) in projecao["posts_reserved"]:
        return True, projecao
    if projecao["remaining_posts"] and projecao["remaining_posts"] > 0:
        return True, projecao
    return False, projecao


def claim_touch(
    root: Path | str, post_id: int, config: Config, *, owner_id: str | None = None,
    now: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Reserva a vaga da sessao de forma ATOMICA (checa E registra).

    ``touch_allowed`` seguido de ``record_touch`` tem uma janela entre a checagem
    e a gravacao: dois processos poderiam passar pelo teto ao mesmo tempo. Como o
    apply chama isto ANTES de escrever, a reserva e o proprio consumo da vaga —
    "post tocado" e o trabalho gasto nele, tenha o resultado sido READY ou nao.
    """
    owner = _validate_owner_id(owner_id or current_owner_id())
    allowed, _ = reserve_touch(root, post_id, config, owner_id=owner, now=now)
    if not allowed:
        return False, status(root, config, now=now)
    return commit_touch(root, post_id, config, owner_id=owner, now=now)


def reserve_touch(
    root: Path | str, post_id: int, config: Config, *, owner_id: str | None = None,
    now: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Reserve future work without consuming ``posts_touched``."""
    owner = _validate_owner_id(owner_id or current_owner_id())
    with _ledger_lock(root):
        data = load_session(root, config, now=now)
        projection = _projection(data, config)
        post_id = int(post_id)
        already_touched = post_id in projection["posts_touched"]
        recovery_proof = ""
        if post_id in projection["posts_reserved"]:
            existing = data["reservation_owners"].get(str(post_id))
            if existing and existing.get("owner_id") == owner:
                return True, projection
            if not existing:
                return False, {**projection, "error_code": "reservation_owner_unverifiable"}
            recovery_proof = _reservation_owner_recovery_proof(existing) or ""
            if not recovery_proof:
                return False, {**projection, "error_code": "reservation_owned_by_other_executor"}
            data["posts_reserved"] = [pid for pid in data["posts_reserved"] if pid != post_id]
            data["reservation_owners"].pop(str(post_id), None)
            data["reservation_recovery_history"].append({
                "post_id": post_id,
                "owner_id_sha256": hashlib.sha256(
                    str(existing["owner_id"]).encode("utf-8")
                ).hexdigest(),
                "proof": recovery_proof,
                "recovered_at": datetime.fromtimestamp(
                    _agora(now), timezone.utc
                ).isoformat(timespec="seconds"),
            })
            data["reservation_recovery_history"] = data["reservation_recovery_history"][-100:]
            projection = _projection(data, config)
        if not already_touched and cap_ativo(config) and projection["remaining_posts"] == 0:
            if recovery_proof:
                _salvar(root, data, now=now)
            return False, projection
        data["posts_reserved"].append(post_id)
        owner_record = _reservation_owner_record(owner)
        if recovery_proof:
            owner_record["recovered_from_owner_sha256"] = data["reservation_recovery_history"][-1]["owner_id_sha256"]
        data["reservation_owners"][str(post_id)] = owner_record
        _salvar(root, data, now=now)
    return True, status(root, config, now=now)


def commit_touch(
    root: Path | str, post_id: int, config: Config, *, owner_id: str | None = None,
    now: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Commit a reservation when real work starts."""
    owner = _validate_owner_id(owner_id or current_owner_id())
    with _ledger_lock(root):
        data = load_session(root, config, now=now)
        post_id = int(post_id)
        reservation = data["reservation_owners"].get(str(post_id))
        if post_id in data["posts_touched"]:
            if (
                post_id not in data["posts_reserved"]
                or not reservation
                or reservation.get("owner_id") != owner
            ):
                return False, _projection(data, config)
            data["posts_reserved"] = [pid for pid in data["posts_reserved"] if pid != post_id]
            data["reservation_owners"].pop(str(post_id), None)
            _salvar(root, data, now=now)
            return True, _projection(data, config)
        if post_id not in data["posts_reserved"] or not reservation or reservation.get("owner_id") != owner:
            return False, _projection(data, config)
        other_reserved = [pid for pid in data["posts_reserved"] if pid != post_id]
        if cap_ativo(config) and len(set(data["posts_touched"]) | set(other_reserved)) >= int(config.max_posts_touched_per_run):
            return False, _projection(data, config)
        data["posts_touched"].append(post_id)
        data["posts_reserved"] = [pid for pid in data["posts_reserved"] if pid != post_id]
        data["reservation_owners"].pop(str(post_id), None)
        _salvar(root, data, now=now)
    return True, status(root, config, now=now)


def release_reservation(
    root: Path | str, post_id: int, config: Config, *, owner_id: str | None = None,
    now: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Release work that was reserved but never started."""
    owner = _validate_owner_id(owner_id or current_owner_id())
    with _ledger_lock(root):
        data = load_session(root, config, now=now)
        post_id = int(post_id)
        reservation = data["reservation_owners"].get(str(post_id))
        if post_id not in data["posts_reserved"]:
            return True, _projection(data, config)
        if not reservation or reservation.get("owner_id") != owner:
            return False, _projection(data, config)
        data["posts_reserved"] = [pid for pid in data["posts_reserved"] if pid != post_id]
        data["reservation_owners"].pop(str(post_id), None)
        _salvar(root, data, now=now)
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
    if (
        cap_ativo(config)
        and projecao["remaining_posts"] == 0
        and not projecao["posts_reserved_capacity_count"]
    ):
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
    "reserve_touch",
    "commit_touch",
    "release_reservation",
    "status",
    "stop_reason",
    "touch_allowed",
]
