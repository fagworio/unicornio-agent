"""Pure V2 outcome classifier; the only module allowed to choose an outcome."""

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


def _failures(validation: dict[str, Any]) -> list[tuple[BlockerCode, str]]:
    result: list[tuple[BlockerCode, str]] = []
    for failure in validation.get("failures", []) or []:
        if not isinstance(failure, dict):
            continue
        blocker = blocker_for_gate(failure.get("gate"))
        if blocker is not None:
            result.append((blocker, str(failure.get("gate"))))
        elif failure.get("blocker"):
            try:
                result.append((BlockerCode(failure["blocker"]), str(failure.get("gate", ""))))
            except ValueError:
                result.append((BlockerCode.INTERNAL_ERROR, str(failure.get("gate", ""))))
        else:
            result.append((BlockerCode.INTERNAL_ERROR, str(failure.get("gate", ""))))
    return result


def classify(previous: WorkState, editorial: dict[str, Any], media: Any, validation: dict[str, Any]) -> Outcome:
    """Classify one completed pipeline attempt without side effects."""
    decision = editorial.get("decision")
    if decision == "skip":
        return Outcome.skipped()
    if decision == "uncertain":
        if previous.retry.attempts >= 1:
            return Outcome.human_required(Phase.RELEVANCE, BlockerCode.RELEVANCE_UNCERTAIN)
        return Outcome.retry(Phase.RELEVANCE, BlockerCode.RELEVANCE_UNCERTAIN)
    if decision != "process":
        return Outcome.human_required(Phase.RELEVANCE, BlockerCode.INTERNAL_ERROR)

    failures = _failures(validation)
    if not failures and validation.get("passed", False):
        return Outcome.ready()
    if not failures:
        return Outcome.retry(Phase.VALIDATE, BlockerCode.INTERNAL_ERROR)

    blockers = [blocker for blocker, _ in failures]
    for blocker in blockers:
        if blocker not in MEDIA_BLOCKERS:
            phase = Phase.EDITORIAL if blocker in {
                BlockerCode.TEXT_QUALITY, BlockerCode.SEO, BlockerCode.STRUCTURE,
                BlockerCode.SOURCE, BlockerCode.TRAILER, BlockerCode.SCHEMA,
            } else Phase.VALIDATE
            return Outcome.retry(phase, blocker)
    return Outcome.retry(Phase.MEDIA, blockers[0])
