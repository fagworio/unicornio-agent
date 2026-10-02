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


def blocker_for_gate(gate: str | None) -> BlockerCode | None:
    return GATE_TO_BLOCKER.get(str(gate or ""))


def _failures(validation: dict[str, Any]) -> list[tuple[BlockerCode, str, Phase | None]]:
    result: list[tuple[BlockerCode, str, Phase | None]] = []
    for failure in validation.get("failures", []) or []:
        if not isinstance(failure, dict):
            continue
        phase_value = None
        if failure.get("phase"):
            try:
                phase_value = Phase(failure["phase"])
            except ValueError:
                phase_value = None
        blocker = blocker_for_gate(failure.get("gate"))
        if blocker is not None:
            result.append((blocker, str(failure.get("gate")), phase_value))
        elif failure.get("blocker"):
            try:
                result.append((BlockerCode(failure["blocker"]), str(failure.get("gate", "")), phase_value))
            except ValueError:
                result.append((BlockerCode.INTERNAL_ERROR, str(failure.get("gate", "")), phase_value))
        else:
            result.append((BlockerCode.INTERNAL_ERROR, str(failure.get("gate", "")), phase_value))
    return result


def _retry(previous: WorkState, phase: Phase, blocker: BlockerCode, next_at: str | None = None) -> Outcome:
    if next_at is None:
        delay = 30 * (4 ** min(previous.retry.attempts, 3))
        next_at = (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat(timespec="seconds")
    return Outcome.retry(phase, blocker, next_at)


def classify(previous: WorkState, editorial: dict[str, Any], media: Any, validation: dict[str, Any]) -> Outcome:
    """Classify one completed pipeline attempt without side effects."""
    decision = editorial.get("decision")
    if decision == "skip":
        return Outcome.skipped()
    if decision == "uncertain":
        if previous.retry.attempts >= 1:
            return Outcome.human_required(Phase.RELEVANCE, BlockerCode.RELEVANCE_UNCERTAIN)
        return _retry(previous, Phase.RELEVANCE, BlockerCode.RELEVANCE_UNCERTAIN)
    if decision != "process":
        return Outcome.human_required(Phase.RELEVANCE, BlockerCode.INTERNAL_ERROR)

    failures = _failures(validation)
    if not failures and validation.get("passed", False):
        return Outcome.ready()
    if not failures:
        return _retry(previous, Phase.VALIDATE, BlockerCode.INTERNAL_ERROR)

    blockers = [blocker for blocker, _, _ in failures]
    for blocker, _, phase_override in failures:
        if blocker not in MEDIA_BLOCKERS:
            phase = phase_override or (Phase.MEDIA if blocker in {BlockerCode.PROVIDER_ERROR, BlockerCode.WORDPRESS_ERROR, BlockerCode.MEDIA_ORIGIN} else (Phase.EDITORIAL if blocker in {
                BlockerCode.TEXT_QUALITY, BlockerCode.SEO, BlockerCode.STRUCTURE,
                BlockerCode.SOURCE, BlockerCode.TRAILER, BlockerCode.SCHEMA,
            } else Phase.VALIDATE))
            return _retry(previous, phase, blocker)
    return _retry(previous, Phase.MEDIA, blockers[0])
