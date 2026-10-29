"""Pure V2 outcome classifier; the only module allowed to choose an outcome."""

from datetime import datetime, timedelta, timezone
from typing import Any

from .model import (
    BlockerCode,
    Outcome,
    OutcomeType,
    Phase,
    WorkState,
    media_search_is_incomplete,
)

GATE_TO_BLOCKER: dict[str, BlockerCode] = {
    "imagens_no_corpo": BlockerCode.INLINE_MISSING,
    "imagens_webp": BlockerCode.MEDIA_INVALID,
    "relevancia_imagens": BlockerCode.MEDIA_INVALID,
    "imagens_duplicadas": BlockerCode.MEDIA_DUPLICATE,
    "imagens_similares": BlockerCode.MEDIA_DUPLICATE,
    "media_duplicate_confirmed": BlockerCode.MEDIA_DUPLICATE,
    "visual_identity_verified": BlockerCode.MEDIA_INVALID,
    "visual_identity_matches_current_media": BlockerCode.MEDIA_INVALID,
    "featured_identity_matches_current_attachment": BlockerCode.FEATURED_INVALID,
    "featured_inline_position": BlockerCode.MEDIA_DUPLICATE,
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
    "idioma_pt_br": BlockerCode.TEXT_QUALITY,
    "seo": BlockerCode.SEO,
    "estrutura": BlockerCode.STRUCTURE,
    "html_semantics": BlockerCode.STRUCTURE,
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

OPERATIONAL_BLOCKERS = frozenset({
    BlockerCode.PROVIDER_ERROR,
    BlockerCode.WORDPRESS_ERROR,
    BlockerCode.INTERNAL_ERROR,
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
        failure_class = str(failure.get("failure_class") or "").strip()
        if failure_class:
            detail = f"{detail}; failure_class={failure_class}" if detail else f"failure_class={failure_class}"
        if "retryable" in failure and isinstance(failure.get("retryable"), bool):
            retryable = "true" if failure["retryable"] else "false"
            detail = f"{detail}; retryable={retryable}" if detail else f"retryable={retryable}"
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
        if blocker in OPERATIONAL_BLOCKERS:
            # Operational failures get a longer window than a
            # media/editorial correction and exponential growth is bounded so
            # a transient outage or internal fault does not hot-loop.
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
    failure_class = str(getattr(exc, "failure_class", "") or "").strip()
    if failure_class:
        detail = f"{detail}; failure_class={failure_class}"
    retryable = getattr(exc, "retryable", None)
    if isinstance(retryable, bool):
        detail = f"{detail}; retryable={'true' if retryable else 'false'}"
    if getattr(exc, "blocker", None) in OPERATIONAL_BLOCKERS:
        return _retry(
            previous,
            exc.phase,
            exc.blocker,
            now=now,
            detail=detail,
            cooldown_minutes=cooldown_minutes,
        )
    if getattr(exc, "human_required", False):
        return Outcome.human_required(exc.phase, exc.blocker, detail=detail)
    if getattr(exc, "blocker", None) in MEDIA_BLOCKERS:
        effective_no_progress = previous.retry.no_progress if no_progress is None else no_progress
        if effective_no_progress >= max(1, max_media_no_progress):
            return Outcome.human_required(
                exc.phase,
                exc.blocker,
                detail=(
                    f"{detail}; media_no_progress_exhausted="
                    f"{effective_no_progress}/{max(1, max_media_no_progress)}"
                ),
            )
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
    failures = _failures(validation)
    if decision == "skip":
        return Outcome.skipped()
    if decision == "uncertain" and not failures:
        if previous.retry.attempts >= 1:
            return Outcome.human_required(Phase.RELEVANCE, BlockerCode.RELEVANCE_UNCERTAIN)
        return _retry(previous, Phase.RELEVANCE, BlockerCode.RELEVANCE_UNCERTAIN, now=now, cooldown_minutes=cooldown_minutes)
    if decision != "process" and not failures:
        return Outcome.human_required(Phase.RELEVANCE, BlockerCode.INTERNAL_ERROR)

    if not failures and validation.get("passed", False):
        return Outcome.ready()
    if not failures:
        if previous.retry.attempts + 1 >= max(1, max_rework_attempts):
            return Outcome.human_required(Phase.VALIDATE, BlockerCode.INTERNAL_ERROR)
        return _retry(previous, Phase.VALIDATE, BlockerCode.INTERNAL_ERROR, now=now, cooldown_minutes=cooldown_minutes)

    blockers = [blocker for blocker, _, _, _ in failures]
    search = getattr(media, "search", None)
    search_reason = str(getattr(search, "completion_reason", "") or "")
    search_coverage = getattr(search, "coverage", None)
    search_coverage = search_coverage if isinstance(search_coverage, dict) else {}
    source_discovery = search_coverage.get("source_discovery")
    source_retryable = (
        isinstance(source_discovery, dict)
        and source_discovery.get("retryable") is True
    )
    source_reason = (
        str(source_discovery.get("reason_code") or "")
        if isinstance(source_discovery, dict)
        else ""
    )
    provider_search_incomplete = media_search_is_incomplete(search)
    if (
        blockers[0] in MEDIA_BLOCKERS
        and search is not None
        and (provider_search_incomplete or source_retryable)
    ):
        # Missing media after an incomplete provider/source-verification run is
        # operationally unresolved, not proof that the article is impossible.
        # Keep the no-progress budget untouched, but bound automatic retries
        # with the existing per-phase attempt budget so a permanently broken
        # provider cannot keep the same post in an unattended retry loop.
        media_phase_attempt = _phase_attempts(previous, Phase.MEDIA) + 1
        if media_phase_attempt >= max(1, max_rework_attempts):
            return Outcome.human_required(
                Phase.MEDIA,
                BlockerCode.PROVIDER_ERROR,
                detail=(
                    f"media acquisition remained incomplete after "
                    f"{media_phase_attempt}/{max(1, max_rework_attempts)} "
                    "phase attempts; no-progress budget was preserved"
                ),
            )
        detail = (
            f"article source discovery retryable ({source_reason})"
            if source_retryable
            else f"media acquisition incomplete ({search_reason or 'unknown'})"
        )
        return _retry(
            previous,
            Phase.MEDIA,
            BlockerCode.PROVIDER_ERROR,
            now=now,
            detail=detail,
            cooldown_minutes=cooldown_minutes,
        )
    operational_failure = next(
        (
            (blocker, phase_override, failure_detail)
            for blocker, _gate, phase_override, failure_detail in failures
            if blocker in OPERATIONAL_BLOCKERS
        ),
        None,
    )
    if operational_failure is not None:
        blocker, phase_override, failure_detail = operational_failure
        phase = phase_override or {
            BlockerCode.PROVIDER_ERROR: Phase.MEDIA,
            BlockerCode.WORDPRESS_ERROR: Phase.PUBLISH,
            BlockerCode.INTERNAL_ERROR: Phase.VALIDATE,
        }[blocker]
        return _retry(
            previous,
            phase,
            blocker,
            now=now,
            detail=failure_detail or None,
            cooldown_minutes=cooldown_minutes,
        )
    first_blocker, _, _, first_detail = failures[0]
    detail: str | None = first_detail or None
    effective_no_progress = previous.retry.no_progress if no_progress is None else no_progress
    if first_blocker in MEDIA_BLOCKERS and getattr(media, "visual_identity_pending", False):
        # A recoverable visual-identity outage is technical uncertainty, not
        # evidence that media enrichment has been exhausted. Keep it retryable
        # even when the editorial no-progress/listicle budgets are already met.
        return _retry(
            previous,
            Phase.MEDIA,
            BlockerCode.PROVIDER_ERROR,
            now=now,
            detail="visual identity verification temporarily unavailable",
            cooldown_minutes=cooldown_minutes,
        )
    if (
        first_blocker in MEDIA_BLOCKERS
        and effective_no_progress >= max(1, max_media_no_progress)
    ):
        exhausted_detail = (
            f"{detail}; media_no_progress_exhausted="
            f"{effective_no_progress}/{max(1, max_media_no_progress)}"
            if detail
            else "media_no_progress_exhausted="
            f"{effective_no_progress}/{max(1, max_media_no_progress)}"
        )
        return Outcome.human_required(
            Phase.MEDIA,
            first_blocker,
            detail=exhausted_detail,
        )
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
    media_decision = {}
    if isinstance(validation, dict):
        checklist = validation.get("checklist") or {}
        media_decision = checklist.get("media_decision") or validation.get("media_decision") or {}
    try:
        enrichment_round = int(getattr(media, "enrichment_round", 0) or 0)
    except (TypeError, ValueError):
        enrichment_round = 0
    # A listicle can become HUMAN_REQUIRED after its second completed
    # enrichment round even when the search itself made progress. For every
    # article, the no-progress budget above prevents an unchanged media
    # blocker from returning to the queue indefinitely.
    if (
        first_blocker in MEDIA_BLOCKERS
        and bool(media_decision.get("is_listicle"))
        and enrichment_round >= 2
    ):
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
