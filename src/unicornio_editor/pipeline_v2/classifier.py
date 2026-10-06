"""Pure V2 outcome classifier; the only module allowed to choose an outcome."""

from datetime import datetime, timedelta, timezone
from typing import Any

from .model import BlockerCode, Outcome, OutcomeType, Phase, WorkState

GATE_TO_BLOCKER: dict[str, BlockerCode] = {
    "imagens_no_corpo": BlockerCode.INLINE_MISSING,
    "imagens_webp": BlockerCode.MEDIA_INVALID,
    "relevancia_imagens": BlockerCode.MEDIA_INVALID,
    "imagens_duplicadas": BlockerCode.MEDIA_DUPLICATE,
    "imagens_similares": BlockerCode.MEDIA_DUPLICATE,
    "destaque_1280x720": BlockerCode.FEATURED_INVALID,
    "destaque_relevancia": BlockerCode.FEATURED_INVALID,
    "imagem_destaque": BlockerCode.FEATURED_MISSING,
    "trailer_youtube": BlockerCode.TRAILER,
    "schema_editorial": BlockerCode.SCHEMA,
    "estrutura_lista": BlockerCode.STRUCTURE,
    "dimensoes_imagens": BlockerCode.MEDIA_INVALID,
    "conteudo_nao_vazio": BlockerCode.TEXT_QUALITY,
    "conteudo_sem_metadados_operacionais": BlockerCode.TEXT_QUALITY,
    "relevancia": BlockerCode.RELEVANCE_UNCERTAIN,
    "fonte_original_link": BlockerCode.SOURCE,
    "cta_canonico": BlockerCode.STRUCTURE,
    "backup": BlockerCode.MANIFEST_INVALID,
    "status_pending": BlockerCode.WORDPRESS_ERROR,
    "imagens_visao": BlockerCode.FEATURED_VISION,
    "qualidade_texto": BlockerCode.TEXT_QUALITY,
    "seo": BlockerCode.SEO,
    "estrutura": BlockerCode.STRUCTURE,
    "fonte": BlockerCode.SOURCE,
    "trailer": BlockerCode.TRAILER,
    "schema": BlockerCode.SCHEMA,
}

MEDIA_BLOCKERS = frozenset({
    BlockerCode.INLINE_MISSING,
    BlockerCode.MEDIA_INVALID,
    BlockerCode.MEDIA_DUPLICATE,
    BlockerCode.FEATURED_MISSING,
    BlockerCode.FEATURED_INVALID,
    BlockerCode.FEATURED_VISION,
    BlockerCode.MEDIA_ORIGIN,
})

EDITORIAL_BLOCKERS = frozenset({
    BlockerCode.TEXT_QUALITY,
    BlockerCode.SEO,
    BlockerCode.STRUCTURE,
    BlockerCode.SOURCE,
    BlockerCode.SCHEMA,
})


def blocker_for_gate(gate: str | None) -> BlockerCode | None:
    return GATE_TO_BLOCKER.get(str(gate or ""))


def _phase_attempts(previous: WorkState, phase: Phase) -> int:
    return previous.retry.phase_attempts if previous.phase is phase else 0


def _editorial_attempt(previous: WorkState, phase: Phase) -> int:
    return _phase_attempts(previous, phase) + 1


def _failures(validation: dict[str, Any]) -> list[tuple[BlockerCode, str, Phase | None, str]]:
    result: list[tuple[BlockerCode, str, Phase | None, str]] = []
    for failure in validation.get("failures", []) or []:
        if not isinstance(failure, dict):
            continue
        phase_value = None
        if failure.get("phase"):
            try:
                phase_value = Phase(failure["phase"])
            except ValueError:
                phase_value = None
        detail = str(failure.get("detail") or "")
        blocker = None
        if failure.get("blocker"):
            try:
                blocker = BlockerCode(failure["blocker"])
            except ValueError:
                blocker = BlockerCode.INTERNAL_ERROR
        if blocker is None:
            blocker = blocker_for_gate(failure.get("gate"))
        if blocker is not None:
            result.append((blocker, str(failure.get("gate")), phase_value, detail))
        elif failure.get("blocker"):
            try:
                result.append((BlockerCode(failure["blocker"]), str(failure.get("gate", "")), phase_value, detail))
            except ValueError:
                result.append((BlockerCode.INTERNAL_ERROR, str(failure.get("gate", "")), phase_value, detail))
        else:
            result.append((BlockerCode.INTERNAL_ERROR, str(failure.get("gate", "")), phase_value, detail))
    return result


def _retry(
    previous: WorkState,
    phase: Phase,
    blocker: BlockerCode,
    next_at: str | None = None,
    now: datetime | None = None,
    detail: str | None = None,
    cooldown_minutes: int = 30,
    backoff_attempts: int | None = None,
) -> Outcome:
    if next_at is None:
        now = now or datetime.now(timezone.utc)
        attempts = previous.retry.attempts if backoff_attempts is None else max(0, backoff_attempts)
        if blocker in {BlockerCode.PROVIDER_ERROR, BlockerCode.WORDPRESS_ERROR}:
            # Provider/network failures are operational incidents. They get a
            # longer window than a media/editorial correction and exponential
            # growth is bounded so a transient outage does not hot-loop.
            delay = max(cooldown_minutes, 120) * (2 ** min(attempts, 3))
        elif blocker in MEDIA_BLOCKERS:
            delay = max(cooldown_minutes, 1) * (2 ** min(attempts, 3))
        elif blocker is BlockerCode.RELEVANCE_UNCERTAIN:
            delay = max(cooldown_minutes, 30)
        else:
            delay = max(cooldown_minutes, 1) * (4 ** min(attempts, 3))
        next_at = (now + timedelta(minutes=delay)).isoformat(timespec="seconds")
    return Outcome(OutcomeType.RETRY, phase, blocker, next_at, detail)


def editorial_decision(editorial: dict[str, Any]) -> str | None:
    decision = editorial.get("decision")
    if decision:
        return str(decision)
    relevance = editorial.get("site_relevance") or {}
    return relevance.get("decision")


def classify_stage_error(
    previous: WorkState,
    exc: Any,
    *,
    now: datetime | None = None,
    max_attempts: int = 3,
    cooldown_minutes: int = 30,
    max_media_no_progress: int = 2,
    max_rework_attempts: int | None = None,
    no_progress: int | None = None,
) -> Outcome:
    if max_rework_attempts is not None:
        max_attempts = max_rework_attempts
    detail = str(getattr(exc, "detail", "") or exc)
    if getattr(exc, "human_required", False):
        return Outcome.human_required(exc.phase, exc.blocker, detail=detail)
    if getattr(exc, "blocker", None) in MEDIA_BLOCKERS:
        return _retry(
            previous,
            exc.phase,
            exc.blocker,
            now=now,
            detail=detail,
            cooldown_minutes=cooldown_minutes,
            backoff_attempts=no_progress,
        )
    if getattr(exc, "blocker", None) in EDITORIAL_BLOCKERS:
        phase_attempt = _editorial_attempt(previous, exc.phase)
        if phase_attempt >= max(1, max_attempts):
            return Outcome.human_required(exc.phase, exc.blocker, detail=detail)
        return _retry(
            previous,
            exc.phase,
            exc.blocker,
            now=now,
            detail=detail,
            cooldown_minutes=cooldown_minutes,
            backoff_attempts=max(0, phase_attempt - 1),
        )
    if previous.retry.attempts + 1 >= max(1, max_attempts):
        return Outcome.human_required(exc.phase, exc.blocker, detail=detail)
    return _retry(previous, exc.phase, exc.blocker, now=now, detail=detail, cooldown_minutes=cooldown_minutes)


def classify(
    previous: WorkState,
    editorial: dict[str, Any],
    media: Any,
    validation: dict[str, Any],
    *,
    now: datetime | None = None,
    no_progress: int | None = None,
    max_media_no_progress: int = 2,
    max_rework_attempts: int = 3,
    cooldown_minutes: int = 30,
) -> Outcome:
    """Classify one completed pipeline attempt without side effects."""
    decision = editorial_decision(editorial)
    if decision == "skip":
        return Outcome.skipped()
    if decision == "uncertain":
        if previous.retry.attempts >= 1:
            return Outcome.human_required(Phase.RELEVANCE, BlockerCode.RELEVANCE_UNCERTAIN)
        return _retry(previous, Phase.RELEVANCE, BlockerCode.RELEVANCE_UNCERTAIN, now=now, cooldown_minutes=cooldown_minutes)
    if decision != "process":
        return Outcome.human_required(Phase.RELEVANCE, BlockerCode.INTERNAL_ERROR)

    failures = _failures(validation)
    if not failures and validation.get("passed", False):
        return Outcome.ready()
    if not failures:
        if previous.retry.attempts + 1 >= max(1, max_rework_attempts):
            return Outcome.human_required(Phase.VALIDATE, BlockerCode.INTERNAL_ERROR)
        return _retry(previous, Phase.VALIDATE, BlockerCode.INTERNAL_ERROR, now=now, cooldown_minutes=cooldown_minutes)

    blockers = [blocker for blocker, _, _, _ in failures]
    first_blocker, _, _, first_detail = failures[0]
    detail: str | None = first_detail or None
    effective_no_progress = previous.retry.no_progress if no_progress is None else no_progress
    for blocker, _, phase_override, failure_detail in failures:
        if phase_override is not None:
            editorial_attempt = (
                _editorial_attempt(previous, phase_override)
                if blocker in EDITORIAL_BLOCKERS or phase_override is Phase.EDITORIAL
                else None
            )
            if editorial_attempt is not None and editorial_attempt >= max(1, max_rework_attempts):
                return Outcome.human_required(phase_override, blocker, detail=failure_detail or None)
            if blocker not in MEDIA_BLOCKERS and editorial_attempt is None and previous.retry.attempts + 1 >= max(1, max_rework_attempts):
                return Outcome.human_required(phase_override, blocker, detail=failure_detail or None)
            return _retry(
                previous,
                phase_override,
                blocker,
                now=now,
                detail=failure_detail or None,
                cooldown_minutes=cooldown_minutes,
                backoff_attempts=(
                    effective_no_progress
                    if blocker in MEDIA_BLOCKERS
                    else max(0, editorial_attempt - 1)
                    if editorial_attempt is not None
                    else None
                ),
            )
        if blocker not in MEDIA_BLOCKERS:
            if blocker is BlockerCode.RELEVANCE_UNCERTAIN:
                phase = Phase.RELEVANCE
            elif blocker in {BlockerCode.PROVIDER_ERROR, BlockerCode.WORDPRESS_ERROR, BlockerCode.MEDIA_ORIGIN}:
                phase = Phase.MEDIA
            elif blocker is BlockerCode.TRAILER:
                phase = Phase.COMPOSE
            elif blocker in {
                BlockerCode.TEXT_QUALITY, BlockerCode.SEO, BlockerCode.STRUCTURE,
                BlockerCode.SOURCE, BlockerCode.SCHEMA,
            }:
                phase = Phase.EDITORIAL
            else:
                phase = Phase.VALIDATE
            editorial_attempt = (
                _editorial_attempt(previous, phase)
                if blocker in EDITORIAL_BLOCKERS or phase is Phase.EDITORIAL
                else None
            )
            if editorial_attempt is not None and editorial_attempt >= max(1, max_rework_attempts):
                return Outcome.human_required(phase, blocker, detail=failure_detail or None)
            if blocker not in MEDIA_BLOCKERS and editorial_attempt is None and previous.retry.attempts + 1 >= max(1, max_rework_attempts):
                return Outcome.human_required(phase, blocker, detail=failure_detail or None)
            return _retry(
                previous,
                phase,
                blocker,
                now=now,
                detail=failure_detail or None,
                cooldown_minutes=cooldown_minutes,
                backoff_attempts=(
                    effective_no_progress
                    if blocker in MEDIA_BLOCKERS
                    else max(0, editorial_attempt - 1)
                    if editorial_attempt is not None
                    else None
                ),
            )
    if effective_no_progress >= max(1, max_media_no_progress):
        return Outcome.human_required(Phase.MEDIA, first_blocker, detail=detail)
    return _retry(
        previous,
        Phase.MEDIA,
        first_blocker,
        now=now,
        detail=detail,
        cooldown_minutes=cooldown_minutes,
        backoff_attempts=effective_no_progress,
    )
