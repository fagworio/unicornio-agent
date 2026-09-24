"""Command-line entry point for the editorial agent."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .backup import SnapshotStore
from .batch import (
    load_editorial_batch,
    load_media_resolve_batch,
    load_vision_batch,
    prepare_batch,
)
from .checklist import required_image_count, run_pre_publish_checklist
from .config import ConfigError, load_config
from .editorial_schema import validate_editorial
from .editorial_provider import EditorialProviderError
from .maintenance import generate_report
from .workflow import (
    WorkflowError,
    apply_editorial,
    attach_trailer_audit,
    build_cards,
    build_queue_report,
    compose_final_content,
    discard_post,
    get_cleaned_content,
    load_draft,
    mark_uncertain,
    original_link_of,
    prepare_post,
    publish_post,
    publish_ready_posts,
    resolve_editorial_defaults,
    retry_post,
    validate_media_plan,
)
from .state import STATE_AWAITING_HUMAN, STATE_BLOCKED, read_state
from .media.text import sanitize_title
from .wordpress import WordPressClient, WordPressError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unicornio-editor",
        description="Processa posts WordPress pending com segurança.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list-pending", help="lista posts pending")
    list_parser.add_argument("--page", type=int, default=1)
    list_parser.add_argument(
        "--compact",
        action="store_true",
        help="imprime apenas id/titulo/contagem de palavras (economia de tokens); "
        "o conteudo completo nao e necessario para escolher o proximo post",
    )

    queue_parser = subparsers.add_parser(
        "queue",
        help="estado deterministico da fila: pending x ja editados (somente leitura)",
    )
    queue_parser.add_argument("--root", type=Path, default=Path("."))
    queue_parser.add_argument(
        "--monitor",
        action="store_true",
        help="imprime apenas a linha estavel (ids pending NAO processados, ou '0'); "
        "usada pelo monitor_script do cron para nao acordar o LLM em ticks ociosos",
    )
    queue_parser.add_argument(
        "--compact",
        action="store_true",
        help="projecao enxuta dos posts (id/titulo/estado/tentativas/ultimo erro) — "
        "economia de tokens: corta os campos redundantes do modo cheio",
    )

    telemetry_parser = subparsers.add_parser(
        "telemetry",
        help="resumo agregado das blocagens/resultados do pipeline (work/telemetry.jsonl; "
        "somente leitura) — responde 'a fila parou por que?'",
    )
    telemetry_parser.add_argument("--root", type=Path, default=Path("."))
    telemetry_parser.add_argument(
        "--sessions",
        action="store_true",
        help="metricas POR POST e POR SESSAO: cruza work/telemetry.jsonl com o "
        "state.db do Hermes (tokens_per_ready, tokens_per_post_touched, "
        "requests_per_ready, tool_context_bytes_per_ready)",
    )
    telemetry_parser.add_argument("--hours", type=int, default=24, help="janela em horas (default: 24)")
    telemetry_parser.add_argument(
        "--state-db", dest="state_db", type=Path, default=None,
        help="state.db do Hermes (default: ~/.hermes/state.db)",
    )
    telemetry_parser.add_argument(
        "--job-id", dest="job_id", type=str, default="",
        help="id do cron editorial (default: HERMES_EDITORIAL_CRON_JOB_ID)",
    )
    telemetry_parser.add_argument(
        "--batch-id", dest="batch_id", type=str, default="",
        help="filtra a telemetria de um batch específico",
    )
    telemetry_parser.add_argument(
        "--project-root", dest="project_root", type=str, default="",
        help="atribuicao por diretorio quando o banco nao expoe a coluna de job",
    )

    canary_parser = subparsers.add_parser(
        "canary-preflight",
        help="preflight somente leitura para dois posts antes do canary real",
    )
    canary_parser.add_argument("post_ids", nargs=2, type=int, help="exatamente dois posts pending")
    canary_parser.add_argument("--root", type=Path, default=Path("."))

    cards_parser = subparsers.add_parser(
        "cards",
        help="cartoes compactos dos posts pending (entidades, gaps, SEO, imagens, dica de jogo) — "
        "economia de tokens: UMA chamada substitui list-pending+prepare+leituras",
    )
    cards_parser.add_argument("--root", type=Path, default=Path("."))
    cards_parser.add_argument("--limit", type=int, default=None, help="maximo de cartoes (default: EDITOR_BATCH_LIMIT)")
    cards_parser.add_argument(
        "--compact", action="store_true",
        help="mantem apenas campos de decisao/acao por post (economia de contexto)",
    )

    prepare_parser = subparsers.add_parser("prepare", help="cria snapshot e relatório")
    prepare_parser.add_argument("post_id", type=int)
    prepare_parser.add_argument("--root", type=Path, default=Path("."))
    prepare_parser.add_argument(
        "--compact",
        action="store_true",
        help="grava o JSON completo em backups/<id>/prepared.json e imprime apenas "
        "o resumo (economia de tokens); leia o arquivo para obter o cleaned_html",
    )

    prepare_batch_parser = subparsers.add_parser(
        "prepare-batch",
        help="prepara varios posts pending em envelopes independentes para uma run stateless",
    )
    prepare_batch_parser.add_argument(
        "post_ids", nargs="+", type=int,
        help="ids dos posts pending; cada post recebe snapshot e arquivo de contexto proprio",
    )
    prepare_batch_parser.add_argument("--root", type=Path, default=Path("."))
    prepare_batch_parser.add_argument(
        "--batch-id",
        default="",
        help="id auditavel opcional (se omitido, o comando gera um novo)",
    )

    apply_parser = subparsers.add_parser("apply", help="valida e aplica JSON editorial")
    apply_parser.add_argument("post_id", type=int)
    apply_parser.add_argument("editorial_file", type=Path)
    apply_parser.add_argument("--root", type=Path, default=Path("."))
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="valida e mostra o resultado (checklist + preview) sem escrever no WordPress",
    )
    apply_parser.add_argument(
        "--compact",
        action="store_true",
        help="grava o relatorio completo em backups/<id>/apply.latest.json e imprime "
        "apenas o resumo (economia de tokens): success = minimo, failure = so o que corrigir",
    )
    apply_parser.add_argument(
        "--merge-draft",
        dest="merge_draft",
        action="store_true",
        help="trata o arquivo como PATCH PARCIAL do rework: mescla deterministicamente "
        "com backups/<id>/editorial.draft.json (listas substituem, dicionarios mesclam "
        "chave a chave) — o agente envia so o componente corrigido, nunca o artigo inteiro",
    )

    apply_batch_parser = subparsers.add_parser(
        "apply-batch",
        help="aplica um envelope editorial em microbatch, mantendo apply/estado isolados por post",
    )
    apply_batch_parser.add_argument("batch_file", type=Path)
    apply_batch_parser.add_argument("--root", type=Path, default=Path("."))
    apply_batch_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="executa o preflight de cada item sem escrever no WordPress",
    )
    apply_batch_parser.add_argument(
        "--compact",
        action="store_true",
        help="grava auditorias individuais e imprime apenas o resumo do batch",
    )

    checklist_parser = subparsers.add_parser(
        "checklist", help="roda o checklist pre-publicacao (somente leitura)"
    )
    checklist_parser.add_argument("post_id", type=int)
    checklist_parser.add_argument("editorial_file", type=Path)
    checklist_parser.add_argument("--root", type=Path, default=Path("."))
    checklist_parser.add_argument(
        "--compact",
        action="store_true",
        help="imprime apenas {status, failed} (failure-only; economiza tokens)",
    )

    publish_parser = subparsers.add_parser(
        "publish",
        help="publica um post somente se o checklist pre-publicacao passar (gate PUBLISH_ENABLED)",
    )
    publish_parser.add_argument("post_id", type=int)
    publish_parser.add_argument("--root", type=Path, default=Path("."))
    publish_ready_parser = subparsers.add_parser(
        "publish-ready",
        help="publica os pending prontos ate a cota da janela (PUBLISH_LIMIT); silencioso quando nao ha nada",
    )
    publish_ready_parser.add_argument("--root", type=Path, default=Path("."))

    report_parser = subparsers.add_parser("maintenance-report", help="gera diagnóstico sem escrever")
    report_parser.add_argument("report_file", type=Path)
    report_parser.add_argument("--broken-url", action="append", default=[])
    report_parser.add_argument("--min-inline-images", type=int, default=1)

    media_search_parser = subparsers.add_parser(
        "media-search",
        help="busca imagens na Media Library local para reuso (somente leitura; "
        "nunca edita o attachment original)",
    )
    media_search_parser.add_argument("termo", type=str, help="termo de busca (title/alt/caption)")
    media_search_parser.add_argument(
        "--limit", type=int, default=10, help="maximo de candidatos (default: 10)"
    )

    media_similar_parser = subparsers.add_parser(
        "media-similar",
        help="compara imagens por hash perceptual (pHash): aponta quais sao o MESMO frame mesmo com URL/bytes diferentes (somente leitura)",
    )
    media_similar_parser.add_argument("urls", nargs="+", type=str, help="URLs das imagens a comparar")
    media_similar_parser.add_argument(
        "--threshold", type=int, default=6, help="distancia Hamming p/ considerar a MESMA imagem (default: 6)"
    )

    media_search_web_parser = subparsers.add_parser(
        "media-search-web",
        help="descobre candidatos de imagem via buscadores (Bing primario, Google/Yandex "
        "fallback) com filtro de tamanho (somente leitura; index de descoberta, a fonte "
        "e a pagina original)",
    )
    media_search_web_parser.add_argument("termo", type=str, help="termo de busca (ex.: redfall xbox series)")
    media_search_web_parser.add_argument("--size", default="xga", help="classe de tamanho (default: xga = 1024x768)")
    media_search_web_parser.add_argument(
        "--ratio", default="w", help="proporcao (default: w = larga)"
    )
    media_search_web_parser.add_argument(
        "--limit", type=int, default=10,
        help="maximo de candidatos por engine (default: 10; a PARADA e por capacidade "
        "aceita, nao por este numero)",
    )
    media_search_web_parser.add_argument(
        "--needed",
        type=int,
        default=0,
        help="deficit REAL do card ('faltam N imagens'): a busca encerra assim que "
        "houver N candidatos FORTES distintos e so expande entre engines enquanto "
        "faltar capacidade (default: 0 = usa --limit)",
    )
    media_search_web_parser.add_argument(
        "--compact",
        dest="compact",
        action="store_true",
        default=True,
        help="(default) stdout com APENAS o que o agente pode usar; o JSON completo "
        "vai para work/search/<chave>.json",
    )
    media_search_web_parser.add_argument(
        "--full",
        dest="compact",
        action="store_false",
        help="devolve o JSON completo (candidatos + rejeitados) no stdout — so para auditoria",
    )
    media_search_web_parser.add_argument(
        "--article-title",
        dest="article_title",
        type=str,
        default="",
        help="titulo do artigo: acrescenta o tipo de conteudo a busca (ex: "
        "'Pluto' num artigo de animes vira 'Pluto anime')",
    )
    media_search_web_parser.add_argument(
        "--post-id",
        dest="post_id",
        type=int,
        default=0,
        help="post de referencia: o subject da busca vem de post_subjects() "
        "(entidade principal do titulo / H2), nao do termo digitado",
    )
    media_search_web_parser.add_argument(
        "--verify",
        dest="verify",
        action="store_true",
        default=True,
        help="valida a origem de cada candidato antes de devolver (default: ligado)",
    )
    media_search_web_parser.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="devolve os candidatos brutos, sem validar a pagina de origem",
    )
    media_search_web_parser.add_argument(
        "--engine", default="auto",
        help="buscador: auto (rotaciona Bing->Google->Yandex), bing, google, yandex "
        "(default: auto)",
    )
    media_search_web_parser.add_argument("--root", type=Path, default=Path("."))

    media_search_listicle_parser = subparsers.add_parser(
        "media-search-listicle",
        help="busca em lote candidatos para obras de uma lista; cada titulo recebe uma busca "
        "independente em paralelo (uma chamada compacta, somente leitura)",
    )
    media_search_listicle_parser.add_argument(
        "--article-title",
        dest="article_title",
        type=str,
        default="",
        help="titulo do artigo: acrescenta o tipo de conteudo a cada item "
        "(ex: item 'Pluto' num artigo de animes vira 'Pluto anime')",
    )
    media_search_listicle_parser.add_argument(
        "titulos", nargs="+", type=str,
        help='titulos/consultas entre aspas, ex.: "Blue Box temporada 2" "Psyren anime"',
    )
    media_search_listicle_parser.add_argument("--size", default="xga")
    media_search_listicle_parser.add_argument("--ratio", default="w")
    media_search_listicle_parser.add_argument(
        "--limit", type=int, default=3,
        help="maximo de candidatos por titulo (default: 3; mantem a resposta compacta)",
    )
    media_search_listicle_parser.add_argument("--engine", default="auto")
    media_search_listicle_parser.add_argument(
        "--post-id",
        dest="post_id",
        type=int,
        default=0,
        help="id do post da lista: permite registrar no ledger a decisão de CADA "
        "item (antes as listas ficavam fora do cruzamento de qualidade)",
    )
    media_search_listicle_parser.add_argument("--root", type=Path, default=Path("."))

    content_parser = subparsers.add_parser(
        "content",
        help="retorna o cleaned_html do post (somente leitura; use SO quando for "
        "reescrever o texto, em vez de abrir o prepared.json inteiro)",
    )
    content_parser.add_argument("post_id", type=int)
    content_parser.add_argument("--root", type=Path, default=Path("."))
    content_parser.add_argument(
        "--force",
        action="store_true",
        help="le o corpo mesmo quando o fix do card nao pede reescrita (use SO "
        "quando voce DECIDIR reescrever o texto)",
    )

    media_validate_parser = subparsers.add_parser(
        "media-validate",
        help="valida relevancia/capacidade do media_plan e a visao da featured, "
        "sem upload (1 chamada compacta; corrija o plano antes do apply)",
    )
    media_validate_parser.add_argument("editorial_file", type=Path)
    media_validate_parser.add_argument(
        "--post-id", type=int,
        help="ID opcional para validar a promessa do título real do WordPress",
    )
    media_validate_parser.add_argument("--root", type=Path, default=Path("."))
    media_validate_parser.add_argument(
        "--compact",
        dest="compact",
        action="store_true",
        default=True,
        help="(default) contrato do agente: {valid, rejected:[{index,reason}], "
        "capacity, featured}; os detalhes completos da vision/evidencia vao para "
        "work/media-validate/<post>.json",
    )
    media_validate_parser.add_argument(
        "--full",
        dest="compact",
        action="store_false",
        help="devolve o relatorio completo (listicle/featured_vision) no stdout",
    )

    vision_batch_parser = subparsers.add_parser(
        "vision-batch",
        help="valida thumbnails independentes em uma chamada multimodal e atualiza o cache seguro",
    )
    vision_batch_parser.add_argument("batch_file", type=Path)
    vision_batch_parser.add_argument("--root", type=Path, default=Path("."))
    vision_batch_parser.add_argument(
        "--full",
        action="store_true",
        help="inclui detalhes completos no stdout (por padrao grava a auditoria em work/)",
    )

    editorial_generate_parser = subparsers.add_parser(
        "editorial-generate-batch",
        help="gera editorial de 1-2 posts com uma única chamada estruturada ao provider",
    )
    editorial_generate_parser.add_argument("input_file", type=Path)
    editorial_generate_parser.add_argument("--root", type=Path, default=Path("."))
    editorial_generate_parser.add_argument("--output", type=Path, default=None)

    media_resolve_batch_parser = subparsers.add_parser(
        "media-resolve-batch",
        help="resolve candidatos de mídia para até 2 posts em uma etapa determinística",
    )
    media_resolve_batch_parser.add_argument("batch_file", type=Path)
    media_resolve_batch_parser.add_argument("--root", type=Path, default=Path("."))
    media_resolve_batch_parser.add_argument(
        "--compact", action="store_true", help="mantém somente o plano acionável no stdout (padrão)"
    )
    media_resolve_batch_parser.add_argument(
        "--full", action="store_true",
        help="inclui candidatos e rejeições completos no stdout; por padrão retorna plano compacto",
    )

    draft_parser = subparsers.add_parser(
        "draft",
        help="imprime o editorial.draft.json do post (base do rework incremental; "
        "leia SO para corrigir o componente apontado pelo fix do card)",
    )
    draft_parser.add_argument("post_id", type=int)
    draft_parser.add_argument("--root", type=Path, default=Path("."))
    draft_parser.add_argument(
        "--for-fix",
        dest="for_fix",
        action="store_true",
        help="devolve SO o componente que o gate bloqueou (inferido do "
        "editorial.blocked.json) + caminho do draft completo — nao reenvia o artigo "
        "inteiro ao LLM so para corrigir uma imagem",
    )
    draft_parser.add_argument(
        "--component",
        choices=["media", "seo", "text", "trailer"],
        default="",
        help="componente do rework a extrair (default: inferido pelo --for-fix)",
    )

    retry_parser = subparsers.add_parser(
        "retry",
        help="reabre um post AWAITING_HUMAN/BLOCKED (revisao humana): zera tentativas "
        "e cooldown; o post volta a fila de rework — nunca força READY",
    )
    retry_parser.add_argument("post_id", type=int)
    retry_parser.add_argument("--root", type=Path, default=Path("."))

    retry_all_parser = subparsers.add_parser(
        "retry-all",
        help="destrava em lote todos os posts AWAITING_HUMAN/BLOCKED elegiveis "
        "(revisao humana): zera tentativas/cooldown de cada um e volta a fila "
        "de rework — nunca força READY",
    )
    retry_all_parser.add_argument("--root", type=Path, default=Path("."))
    retry_all_parser.add_argument(
        "--states",
        default="awaiting_human,blocked",
        help="estados a destravar (default: awaiting_human,blocked)",
    )

    discard_parser = subparsers.add_parser(
        "discard",
        help="descarta um post da fila editorial (decisao humana): grava uncertain.json "
        "e estado UNCERTAIN — sai da agenda e nunca publica",
    )
    discard_parser.add_argument("post_id", type=int)
    discard_parser.add_argument("--root", type=Path, default=Path("."))
    discard_parser.add_argument("--reason", type=str, default="")

    migrate_parser = subparsers.add_parser(
        "migrate-state",
        help="migration explicita dos posts legados (sem _hermes_state): grave o "
        "estado que falta para o publish-ready exigir SOMENTE ready (dry-run por "
        "padrao)",
    )
    migrate_parser.add_argument("--root", type=Path, default=Path("."))
    migrate_parser.add_argument("--apply", action="store_true",
                                help="sem esta flag o comando apenas relata o que faria")
    migrate_parser.add_argument("--limit", type=int, default=0)

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="compara status WP x _hermes_state x artefatos do filesystem "
        "(somente leitura): reporta divergencias de estado com o reparo sugerido",
    )
    reconcile_parser.add_argument("--root", type=Path, default=Path("."))
    reconcile_parser.add_argument("--limit", type=int, default=100)
    reconcile_parser.add_argument(
        "--statuses",
        type=str,
        default="pending,awaiting_human",
        help="statuses WP a varrer (separados por virgula)",
    )

    uncertain_parser = subparsers.add_parser(
        "uncertain",
        help="registra a decisao do agente de nao processar o post agora (motivo obrigatorio)",
    )
    uncertain_parser.add_argument("post_id", type=int)
    uncertain_parser.add_argument("--root", type=Path, default=Path("."))
    uncertain_parser.add_argument("--reason", type=str, required=True)
    return parser


def _html_word_count(html: str) -> int:
    """Conta palavras do texto de um HTML, ignorando tags."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    return len(text.split())


def _compact_listing(posts: list[dict]) -> list[dict]:
    """Projecao enxuta para list-pending --compact (economia de tokens)."""
    compact: list[dict] = []
    for post in posts:
        title = sanitize_title(
            (post.get("title") or {}).get("raw") or (post.get("title") or {}).get("rendered")
        )
        rendered = (post.get("content") or {}).get("rendered") or ""
        compact.append(
            {
                "id": post.get("id"),
                "status": post.get("status"),
                "date": post.get("date"),
                "title": title,
                "word_count": _html_word_count(rendered),
                "link": post.get("link"),
            }
        )
    return compact


def _compact_queue(report: dict) -> dict:
    """Projecao enxuta do relatorio de fila (economia de tokens).

    O modo cheio despeja ~15 campos por post (date, word_count, booleanos
    redundantes com ``state`` etc.). O agente decide pela fila usando apenas
    id/titulo/estado/tentativas e o ultimo erro; o detalhe por post (imagens,
    featured, fix) vem do ``cards``. Mantem os campos de resumo (contagens +
    listas de ids) intactos.
    """
    compact = dict(report)
    posts = report.get("posts") or []
    compact["posts"] = [
        {
            "id": p.get("id"),
            "title": p.get("title"),
            "state": p.get("state"),
            "attempts": p.get("attempts"),
            "last_error": (p.get("last_error") or "")[:80],
        }
        for p in posts
    ]
    return compact


def _compact_cards(report: dict) -> dict:
    """Action-only cards; ``apply`` re-fetches all authoritative WP fields."""
    cards: list[dict] = []
    for card in report.get("cards") or []:
        row = {
            key: card.get(key)
            for key in (
                "id", "title", "state", "attempts", "seo_exists", "images",
                "featured", "game_hint", "blocked", "requires_content",
            )
        }
        if card.get("blocked"):
            row.update({
                "blocked_reason": card.get("blocked_reason"),
                "fix": card.get("fix"),
                "draft": card.get("draft"),
            })
        cards.append(row)
    return {"count": len(cards), "cards": cards}


def _media_search_item(item: dict) -> dict:
    """Projecao compacta de um candidato da Media Library (economia de tokens).

    ``tem_credito`` informa se o attachment carrega o bloco 'Crédito da
    imagem' no title/caption — sem isso a imagem NAO pode ser reutilizada
    (falta evidencia de licenca). O reuso nunca edita o attachment original.
    """
    title = str((item.get("title") or {}).get("rendered") or "")
    caption = str((item.get("caption") or {}).get("rendered") or "")
    details = item.get("media_details") or {}
    width, height = details.get("width"), details.get("height")
    return {
        "id": item.get("id"),
        "title": title[:120],
        "alt": str(item.get("alt_text") or "")[:120],
        "dimensoes": f"{width or '?'}x{height or '?'}",
        "tem_credito": "crédito da imagem" in f"{title} {caption}".lower(),
        "url": str(item.get("source_url") or ""),
    }


def _failed_items(checklist: dict | None) -> list[dict]:
    """Apenas os itens que falharam (failure-only; economia de tokens)."""
    if not isinstance(checklist, dict):
        return []
    return [
        {"name": item.get("name"), "detail": str(item.get("detail") or "")[:200]}
        for item in (checklist.get("items") or [])
        if item.get("status") in ("fail", "error") and item.get("name")
    ]


def _compact_apply(result: dict) -> dict:
    """Projecao enxuta do apply (Fase 6): success = minimo, failure = so o que corrigir.

    - Sucesso: {post_id, status: ready, wordpress_changed, checklist: pass,
      images: {required, valid}}.
    - needs_rework: {post_id, status, state, attempts, next_retry_at,
      wordpress_changed, failed: [{name, detail, required?, valid?, missing?}]}
      — o delta exato (o que falhou, quanto falta) sem checklist completo.
    O relatorio completo (checklist, midia, trailer, preview) fica em
    ``backups/<id>/apply.latest.json``.
    """
    post_id = result.get("post_id")
    failed = _failed_items(result.get("checklist"))
    media_results = result.get("media_plan_results") or []
    accepted = sum(1 for m in media_results if m.get("media_id"))
    rejected = sum(
        1 for m in media_results if m.get("status") in ("rejected", "blocked")
    )
    images = result.get("images") or {}
    if result.get("status") == "needs_rework":
        reasons: list[dict] = []
        for name in (result.get("blocked_reasons") or []):
            item: dict = {"name": name, "detail": result.get("blocked_detail") or ""}
            if name == "imagens_no_corpo" and images:
                item.update(
                    {
                        "required": images.get("required"),
                        "valid": images.get("valid"),
                        "missing": images.get("missing"),
                    }
                )
            reasons.append(item)
        compact = {
            "post_id": post_id,
            "status": "needs_rework",
            "state": result.get("state"),
            "attempts": result.get("attempts"),
            "next_retry_at": result.get("next_retry_at"),
            "wordpress_changed": bool(result.get("wordpress_changed")),
            "failed": failed or reasons,
        }
        if result.get("baseline_enriched"):
            compact["baseline_enriched"] = True
        if images:
            compact["images"] = images
        return compact
    if result.get("status") == "uncertain":
        compact = {
            "post_id": post_id,
            "status": "uncertain",
            "wordpress_changed": bool(result.get("wordpress_changed")),
            "skip_reason": result.get("skip_reason"),
        }
        if result.get("baseline_enriched"):
            compact["baseline_enriched"] = True
        return compact
    if result.get("status") == "skipped":
        compact = {
            "post_id": post_id,
            "status": "skipped",
            "wordpress_changed": bool(result.get("wordpress_changed")),
            "skip_reason": result.get("skip_reason"),
        }
        if result.get("baseline_enriched"):
            compact["baseline_enriched"] = True
        return compact
    if result.get("dry_run"):
        compact = {
            "post_id": post_id,
            "status": "dry_run",
            "wordpress_changed": False,
            "checklist": "pass" if not failed else "fail",
            "media": {"accepted": accepted, "rejected": rejected},
        }
        if images:
            compact["images"] = {"required": images.get("required"), "valid": images.get("valid")}
        if failed:
            compact["failed"] = failed
        return compact
    compact = {
        "post_id": post_id,
        "status": "ready" if result.get("status") == "ready" else (
            "applied" if result.get("wordpress_changed") else "not_changed"
        ),
        "wordpress_changed": bool(result.get("wordpress_changed")),
        "checklist": "pass" if not failed else "fail",
        "featured_media": result.get("featured_media"),
        "media": {"accepted": accepted, "rejected": rejected},
    }
    if images:
        compact["images"] = {"required": images.get("required"), "valid": images.get("valid")}
    if failed:
        compact["failed"] = failed
    return compact


def _apply_editorial_batch(
    client: WordPressClient,
    config: Any,
    root: Path,
    batch: dict[str, Any],
    *,
    dry_run: bool = False,
    compact: bool = True,
) -> dict[str, Any]:
    """Apply a validated batch one post at a time.

    This is intentionally not a multi-post transaction.  Each item keeps the
    existing snapshot, lock, checklist, state and manifest guarantees.  A
    session budget stops the batch before the next untouched post; an already
    touched post remains eligible for rework, matching the single-post apply.
    """
    from . import session_budget

    batch_id = str(batch["batch_id"])
    outcomes: list[dict[str, Any]] = []
    stopped = ""
    remaining: list[int] = []
    previous_batch = os.environ.get("UNICORNIO_BATCH_ID")
    previous_stage = os.environ.get("UNICORNIO_BATCH_STAGE")
    os.environ["UNICORNIO_BATCH_ID"] = batch_id
    os.environ["UNICORNIO_BATCH_STAGE"] = "apply"
    try:
        for index, item in enumerate(batch["items"]):
            post_id = int(item["post_id"])
            if str(item.get("status") or "ok") == "needs_retry":
                outcomes.append({
                    "post_id": post_id,
                    "status": "needs_retry",
                    "wordpress_changed": False,
                    "reason": str(item.get("reason") or "editorial batch marcou retry")[:240],
                    "action": "gere novamente somente este post",
                })
                continue
            # Retry do mesmo batch é idempotente: um item que já terminou READY
            # não deve ser reaplicado só porque o parceiro do batch falhou.
            if not dry_run:
                prior_path = root / "backups" / str(post_id) / "apply.latest.json"
                try:
                    prior = json.loads(prior_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    prior = {}
                if (
                    isinstance(prior, dict)
                    and prior.get("batch_id") == batch_id
                    and prior.get("status") == "ready"
                ):
                    outcomes.append({
                        "post_id": post_id,
                        "status": "noop",
                        "state": "ready",
                        "wordpress_changed": False,
                        "idempotent": True,
                    })
                    continue
            projection = session_budget.status(root, config)
            already_touched = post_id in projection["posts_touched"]
            reason = session_budget.stop_reason(root, config)
            if dry_run:
                allowed, projection = session_budget.touch_allowed(root, post_id, config)
                no_slot = not allowed
            elif reason and not already_touched:
                no_slot = True
            else:
                allowed, projection = session_budget.claim_touch(root, post_id, config)
                no_slot = not allowed
            if not dry_run and no_slot:
                stopped = reason or (
                    "teto de posts tocados por sessao atingido "
                    f"({projection['posts_touched_count']}/{projection['max_posts_touched']})"
                )
                remaining = [int(row["post_id"]) for row in batch["items"][index:]]
                break

            try:
                result = apply_editorial(
                    client, config, root, post_id, item["editorial"]
                )
                audit = root / "backups" / str(post_id) / "apply.latest.json"
                _write_audit(audit, {**result, "batch_id": batch_id})
                if not result.get("dry_run") and result.get("status") == "ready":
                    session_budget.record_ready(root, config)
                outcomes.append(_compact_apply(result) if compact else result)
            except Exception as exc:  # noqa: BLE001 - isolate one post
                outcomes.append(
                    {
                        "post_id": post_id,
                        "status": "error",
                        "wordpress_changed": False,
                        "error": str(exc)[:240],
                    }
                )
    finally:
        if previous_batch is None:
            os.environ.pop("UNICORNIO_BATCH_ID", None)
        else:
            os.environ["UNICORNIO_BATCH_ID"] = previous_batch
        if previous_stage is None:
            os.environ.pop("UNICORNIO_BATCH_STAGE", None)
        else:
            os.environ["UNICORNIO_BATCH_STAGE"] = previous_stage

    summary = {
        "schema_version": batch["schema_version"],
        "batch_id": batch_id,
        "count": len(batch["items"]),
        "processed": len(outcomes),
        "ready": sum(1 for item in outcomes if item.get("status") == "ready"),
        "noop": sum(1 for item in outcomes if item.get("status") == "noop"),
        "needs_retry": sum(1 for item in outcomes if item.get("status") == "needs_retry"),
        "needs_rework": sum(
            1 for item in outcomes if item.get("status") == "needs_rework"
        ),
        "errors": sum(1 for item in outcomes if item.get("status") == "error"),
        "stopped": stopped,
        "remaining_post_ids": remaining,
        "posts": outcomes,
        "session": session_budget.status(root, config),
    }
    _write_audit(
        root / "work" / "batches" / batch_id / "apply.manifest.json", summary
    )
    return summary


def _resolve_media_batch(
    client: WordPressClient,
    config: Any,
    root: Path,
    batch: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    """Resolve media for all posts without another Hermes/LLM turn.

    Search discovery is parallelized by ``search_web_images_batch``. The
    expensive/safety-sensitive enrichment remains deterministic and isolated
    per post: source-page proof, relevance, pHash and local-library reuse.
    The result is a media plan input for the editorial batch; it never uploads
    or mutates WordPress.
    """
    from .media.search import (
        http_request_count,
        reset_http_request_count,
        search_web_images_batch,
    )
    from .observability import append_telemetry

    posts = batch["posts"]
    reuse_by_post: dict[int, list[dict]] = {}
    needed_web_by_post: dict[int, int] = {}
    searchable = []
    for item in posts:
        post_id = int(item["post_id"])
        reuse = _reuse_from_library(client, root, str(item["subject"]), limit=int(item["needed"]))
        reuse_by_post[post_id] = reuse
        needed_web_by_post[post_id] = max(0, int(item["needed"]) - len(reuse))
        if needed_web_by_post[post_id] > 0:
            searchable.append(item)
    queries = [str(item["query"] or item["subject"]) for item in searchable]
    limit = max((int(item["limit"]) for item in searchable), default=1)
    first = searchable[0] if searchable else posts[0]
    spec_by_query = {str(item["query"] or item["subject"]): item for item in searchable}
    memo_by_query = {query: {} for query in queries}
    accepted_urls_by_query: dict[str, set[str]] = {query: set() for query in queries}

    def accept(novos: list[dict], query: str) -> int:
        spec = spec_by_query.get(str(query))
        if not spec:
            return 0
        post_id = int(spec["post_id"])
        aprovados, _rejeitados, _deferidos = _enriquecer_candidatos(
            novos,
            subject=str(spec["subject"]),
            termo=str(spec["query"]),
            verify=True,
            root=root,
            capacity=needed_web_by_post[post_id],
            enriched_cache=memo_by_query[str(query)],
        )
        accepted_urls_by_query[str(query)].update(
            str(candidate.get("direct_image_url") or "")
            for candidate in aprovados
            if (candidate.get("evidence") or {}).get("verdict") == "deterministic_match"
        )
        return len(accepted_urls_by_query[str(query)])

    reset_http_request_count()
    found = search_web_images_batch(
        queries,
        size=str(first.get("size") or "xga"),
        ratio=str(first.get("ratio") or "w"),
        limit=limit,
        timeout=config.http_timeout,
        engine=str(first.get("engine") or "auto"),
        accept=accept if searchable else None,
    )
    by_query = {str(row.get("query")): row.get("candidates") or [] for row in found}
    output: list[dict[str, Any]] = []
    vision_items: list[dict[str, Any]] = []
    total_candidates = 0
    total_rejected = 0
    for item in posts:
        post_id = int(item["post_id"])
        subject = str(item["subject"])
        needed = int(item["needed"])
        reuso = reuse_by_post[post_id]
        needed_web = needed_web_by_post[post_id]
        candidates = list(by_query.get(str(item["query"]), []))
        aprovados, rejeitados, deferidos = _enriquecer_candidatos(
            candidates,
            subject=subject,
            termo=str(item["query"]),
            verify=True,
            root=root,
            capacity=needed_web,
            enriched_cache=memo_by_query.get(str(item["query"]), {}),
        )
        total_candidates += len(candidates)
        total_rejected += len(rejeitados)
        compact_candidates = [_media_candidate(candidate) for candidate in aprovados]
        for index, candidate in enumerate(aprovados):
            if candidate.get("needs_vision") and len(vision_items) < 20:
                vision_items.append({
                    "candidate_id": f"{post_id}-{index}",
                    "post_id": post_id,
                    "image_url": str(candidate.get("direct_image_url") or ""),
                    "subject": subject,
                    "require_key_art": False,
                })
        output.append({
            "post_id": post_id,
            "subject": subject,
            "needed": needed,
            "reuse": reuso,
            "needed_web": needed_web,
            "candidates": compact_candidates[:needed],
            "deferred": len(deferidos),
            "rejected": len(rejeitados),
            "audit_candidates": aprovados + rejeitados + deferidos,
        })
    result = {
        "schema_version": batch["schema_version"],
        "batch_id": batch["batch_id"],
        "count": len(output),
        "posts": output,
        "vision_items": vision_items,
        "economics": {
            "posts": len(output),
            "search_queries": len(queries),
            "external_http_requests": http_request_count(),
            "candidates_examined": total_candidates,
            "candidates_rejected": total_rejected,
            "llm_requests": 0,
            "tool_calls": 1,
        },
    }
    append_telemetry(
        root,
        "media_resolve_batch",
        batch_size=len(output),
        posts=len(output),
        search_queries=len(queries),
        external_http_requests=http_request_count(),
        candidates_examined=total_candidates,
        candidates_rejected=total_rejected,
        llm_requests=0,
        tool_calls=1,
    )
    audit_payload = {
        **result,
        "posts": [
            {**post, "audit_candidates": post["audit_candidates"]}
            for post in output
        ],
    }
    audit = _write_audit(
        root / "work" / "batches" / batch["batch_id"] / "media.manifest.json",
        audit_payload,
    )
    result["audit"] = audit
    if vision_items:
        result["vision_batch_file"] = _write_audit(
            root / "work" / "batches" / batch["batch_id"] / "vision.input.json",
            {
                "schema_version": batch["schema_version"],
                "batch_id": batch["batch_id"],
                "items": vision_items,
            },
        )
    if not full:
        result["posts"] = [
            {key: value for key, value in post.items() if key != "audit_candidates"}
            for post in output
        ]
    return result


def _canary_preflight(
    client: WordPressClient,
    config: Any,
    root: Path,
    post_ids: list[int],
) -> dict[str, Any]:
    """Run the authenticated, read-only checks required before a canary.

    No snapshot, provider call, upload or WordPress write happens here. The
    report is deliberately explicit so a failed preflight cannot be mistaken
    for a failed editorial batch.
    """
    posts: list[dict[str, Any]] = []
    for post_id in post_ids:
        row: dict[str, Any] = {"post_id": int(post_id), "status": "error"}
        try:
            post = client.get_post(int(post_id))
            title_value = post.get("title") or {}
            title = str(
                title_value.get("raw") or title_value.get("rendered") or ""
            ) if isinstance(title_value, dict) else str(title_value or "")
            row.update({
                "wordpress_status": post.get("status"),
                "state": read_state(post),
                "title": title[:160],
                "featured_media": int(post.get("featured_media") or 0),
            })
            # GET /media is part of the preflight: it verifies that the
            # authenticated application password can read the local library.
            media = client.search_media(title or f"post-{post_id}", per_page=1)
            row["media_library_read"] = True
            row["media_candidates_seen"] = len(media)
            row["status"] = "ready" if post.get("status") == "pending" else "not_pending"
        except Exception as exc:  # noqa: BLE001 - report each post independently
            row["error"] = str(exc)[:240]
        posts.append(row)
    provider = {
        "wordpress_credentials_present": bool(config.app_user and config.app_password),
        "editorial_provider_configured": bool(config.editorial_api_key),
        "vision_provider_configured": bool(not config.vision_enabled or config.vision_api_key),
        "editorial_model": config.editorial_model,
        "vision_model": config.vision_model if config.vision_enabled else None,
        "dry_run": bool(config.dry_run),
    }
    from .observability import read_telemetry_summary

    posts_ok = all(row.get("status") == "ready" for row in posts)
    checks_ok = (
        posts_ok
        and provider["wordpress_credentials_present"]
        and provider["editorial_provider_configured"]
        and provider["vision_provider_configured"]
    )
    return {
        "status": "ready" if checks_ok else "blocked",
        "read_only": True,
        "post_ids": [int(post_id) for post_id in post_ids],
        "provider": provider,
        "posts": posts,
        "baseline_24h": read_telemetry_summary(root, hours=24),
        "next": (
            "execute prepare-batch e editorial-generate-batch; mantenha EDITOR_DRY_RUN=true no primeiro ensaio"
            if checks_ok else
            "corrija autenticação/status/provider antes de executar o canary"
        ),
    }


def _compact_checklist(checklist: dict) -> dict:
    """Checklist failure-only: {status, failed} (detalhes vao para o relatorio)."""
    failed = _failed_items(checklist)
    return {"status": "pass" if not failed else "fail", "failed": failed}


def _monitor_line(report: dict) -> str:
    """Linha ESTAVEL do monitor (hasheada pelo cron).

    So muda quando ha trabalho elegivel real: pending nao processado (id),
    rework BLOCKED fora de cooldown (id) e rework em cooldown codificado
    como id@next_retry_at (minuto). O hash so muda quando um cooldown expira
    (o post troca de grupo cooldown -> elegivel), NUNCA a cada tick por um
    bucket de parede — evita rework eterno queimando tokens.
    """
    parts = [str(pid) for pid in report.get("eligible_rework_ids", [])]
    parts += [str(pid) for pid in report.get("unprocessed_ids", [])]
    in_cooldown = sorted(
        f"{row['id']}@{str(row.get('next_retry_at') or '')[:16]}"
        for row in (report.get("posts") or [])
        if row.get("state") == "blocked" and row.get("next_retry_at")
    )
    parts += in_cooldown
    return " ".join(parts) or "0"



def _enriquecer_candidatos(
    candidates: list[dict],
    *,
    subject: str,
    termo: str,
    verify: bool = True,
    root=None,
    capacity: int | None = None,
    enriched_cache: dict | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Pipeline ÚNICO de mídia: origem -> contexto -> score (Fases 5/10/11).

    Usado pelo ``media-search-web`` (artigo: subject = entidade principal do
    post) e pelo ``media-search-listicle`` (um subject por item/H2). Cada
    candidato é validado contra a SUA página de origem e pontuado contra o SEU
    subject — nenhum herda evidência global do artigo.

    Só ``deterministic_match`` e ``ambiguous`` são aprovados; qualquer veredito
    de gate (``unresolved_source``/``source_mismatch``/``duplicate_frame``) ou
    relevância < 4 vai para rejeitados com o motivo.

    Devolve ``(aprovados, rejeitados, deferidos)``. ``deferidos`` são os
    candidatos que a capacidade já atendida dispensou de investigar: NÃO são
    rejeições (nada foi verificado contra eles) e por isso têm contagem própria —
    misturá-los com os rejeitados faria a busca parecer pior justamente quando
    ficou mais eficiente.

    Dois parâmetros de ECONOMIA (P1 da auditoria de contexto), ambos sem
    afrouxar nenhum gate:

    * ``capacity`` — déficit real do post (quantas imagens faltam). O
      SourceResolver para de investigar candidatos assim que já existem
      ``capacity`` candidatos válidos na página de origem: candidato que já não
      é necessário não é investigado (mantém a ordem da busca e jamais
      transforma candidato sem origem em utilizável).
    * ``enriched_cache`` — memo por ``direct_image_url+source_page_url+subject``.
      O ``media-search-web`` enriquece o lote que encerra a busca E os
      candidatos acumulados no fim; sem cache o mesmo candidato era resolvido,
      pontuado e hasheado DUAS vezes (latência, retries e verificação
      duplicada, sem ganho nenhum).
    """
    from urllib.parse import unquote

    from .media.evidence import evidence_score, source_context
    from .media.official_sources import official_source
    from .media.source_verify import validate_discovered_candidate

    cache_memo = enriched_cache if enriched_cache is not None else {}
    aprovados: list[dict] = []
    rejeitados: list[dict] = []
    deferidos: list[dict] = []
    cache_paginas: dict = {}
    cache_html: dict = {}

    def _memo_key(cand: dict) -> str:
        return "\u0000".join(
            (
                str(cand.get("direct_image_url") or ""),
                str(cand.get("source_page_url") or ""),
                subject,
            )
        )

    # Campos que o enriquecimento produz (é o que o memo guarda/replica).
    _CAMPOS = (
        "source_page_url", "usable", "discovery_only", "rejected_reason",
        "valid", "valid_reason", "images_in_page", "evidence", "evidence_score",
        "needs_vision", "source_context_used", "official_source",
        "already_in_library", "library_media_id", "phash", "capacity_deferred",
    )

    def _aplicar_memo(cand: dict, memo: dict) -> None:
        for campo in _CAMPOS:
            if campo in memo:
                cand[campo] = memo[campo]

    def _guardar_memo(cand: dict, chave: str | None = None) -> None:
        cache_memo[chave or _memo_key(cand)] = {
            campo: cand[campo] for campo in _CAMPOS if campo in cand
        }

    pendentes: list[dict] = []
    for cand in candidates:
        memo = cache_memo.get(_memo_key(cand))
        if memo is not None:
            _aplicar_memo(cand, memo)
            continue
        pendentes.append(cand)
    pendentes_ids = {id(cand) for cand in pendentes}

    # CAPACIDADE REAL do resolver (P0 da revisao 4). `capacity` é "quantas
    # imagens FORTES e DISTINTAS o post precisa". Contar apenas
    # `source_resolution == "verified_page"` ainda parava cedo demais: verified_page
    # prova que a imagem ESTA naquela pagina, nao que ela é relevante para o
    # subject (o evidence_score vem depois) nem que o frame é distinto (o pHash
    # vem depois). Com capacity=1, um candidato A que resolvia a origem e DEPOIS
    # era rejeitado por relevância dispensava B e C — que podiam ser a imagem
    # certa. O pipeline agora roda POR CANDIDATO (resolver -> evidencia -> pHash)
    # e a capacidade só conta forte + frame distinto.
    from .media.visual_hash import image_hashes

    hashes_locais: dict[str, str] = {}

    def _phash_de(url: str) -> str:
        chave = str(url or "")
        if chave and chave not in hashes_locais:
            try:
                hashes_locais.update(image_hashes([chave]))
            except Exception:  # noqa: BLE001 - pHash é melhor-esforço
                pass
        return str(hashes_locais.get(chave) or "")

    def _fortes_distintos() -> int:
        vistos: set[str] = set()
        for forte in aprovados:
            if (forte.get("evidence") or {}).get("verdict") != "deterministic_match":
                continue
            chave = str(forte.get("phash") or "") or str(forte.get("direct_image_url") or "")
            if chave:
                vistos.add(chave)
        return len(vistos)

    def _classificar(cand: dict) -> None:
        """Evidência + proveniência + pHash de UM candidato (fase por candidato)."""
        cand["subject"] = subject
        if cand.get("capacity_deferred"):
            # NÃO é rejeição: nada foi verificado contra este candidato, ele
            # apenas deixou de ser necessário. Contagem própria (deferidos) para
            # a telemetria distinguir "já tínhamos material suficiente" de
            # "investigado e rejeitado".
            cand["evidence"] = {
                "subject": subject, "score": 0, "verdict": "capacity_met",
                "gate": "capacity", "matched": [], "needs_vision": False,
                "reason": "capacidade do post já atendida; candidato não investigado",
            }
            cand["evidence_score"] = 0
            cand["needs_vision"] = False
            deferidos.append(cand)
            _guardar_memo(cand)
            return
        if not cand.get("usable"):
            # Gate A: sem origem nao existe ACCEPT possivel.
            cand["evidence"] = {
                "subject": subject, "score": 0, "verdict": "unresolved_source",
                "gate": "provenance", "matched": [], "needs_vision": False,
                "reason": cand.get("rejected_reason") or "candidato sem origem",
            }
            cand["evidence_score"] = 0
            cand["needs_vision"] = False
            rejeitados.append(cand)
            _guardar_memo(cand)
            return
        if verify:
            veredito = validate_discovered_candidate(
                cand, cache=cache_paginas, cache_html=cache_html
            )
            cand["valid"] = bool(veredito["valid"])
            cand["valid_reason"] = str(veredito.get("reason") or "")
            cand["images_in_page"] = int(veredito.get("images_in_page") or 0)
        pagina = str(cand.get("source_page_url") or "")
        nome = unquote(str(cand.get("direct_image_url") or "").split("?")[0].rsplit("/", 1)[-1])
        ctx = (
            source_context(cache_html.get(pagina, ""), str(cand.get("direct_image_url") or ""),
                           base_url=pagina)
            if pagina
            else {}
        )
        pontos = evidence_score(
            subject,
            filename=nome,
            og_title=ctx.get("og_title", ""),
            page_title=ctx.get("page_title", ""),
            alt_original=ctx.get("alt_original", ""),
            figcaption=ctx.get("figcaption", ""),
            heading=ctx.get("heading", ""),
            page_url=pagina,
            query=termo,
            source_page_present=bool(pagina),
            image_in_source=bool(cand.get("valid")),
        )
        cand["evidence"] = pontos
        cand["evidence_score"] = pontos["score"]
        cand["needs_vision"] = bool(pontos["needs_vision"])
        cand["source_context_used"] = bool(ctx)
        # Estratégia C (registry oficial): imagem servida por domínio do próprio
        # publisher/estúdio é proveniência mais forte — vira desempate na
        # ordenação e evidência auditável no JSON.
        cand["official_source"] = official_source(
            str(cand.get("direct_image_url") or ""), subject
        )
        if pontos["verdict"] == "deterministic_match":
            # pHash imediato: a capacidade do resolver exige frame DISTINTO e,
            # sem o hash aqui, a contagem cairia para URL — o mesmo frame servido
            # por duas engines (URLs diferentes) contaria como dois fortes.
            bruto = _phash_de(str(cand.get("direct_image_url") or ""))
            if bruto:
                cand["phash"] = bruto
        (aprovados if pontos["verdict"] in ("deterministic_match", "ambiguous") else rejeitados).append(cand)
        _guardar_memo(cand)

    teto_resolver = int(capacity) if capacity and capacity > 0 else 0
    resolver = None
    if any(not str(c.get("source_page_url") or "").strip() for c in pendentes):
        try:
            from .media.source_resolver import resolve_candidate_source as resolver  # noqa: PLC0415
        except Exception:  # noqa: BLE001 - resolver é best-effort
            resolver = None

    for cand in pendentes:
        chave_original: str | None = None
        sem_origem = not str(cand.get("source_page_url") or "").strip()
        if sem_origem and resolver is not None:
            if teto_resolver and _fortes_distintos() >= teto_resolver:
                # Capacidade JÁ atendida por fortes distintos: não investiga
                # candidato que não é necessário (economia de rede/latência —
                # o gate não é afrouxado, o candidato só não vira rejeição).
                cand["capacity_deferred"] = True
                cand["rejected_reason"] = "capacity_met"
                _classificar(cand)
                continue
            chave_original = _memo_key(cand)
            # O verifier é a MESMA validação determinística: o resolver testa as
            # páginas em ordem e fica com a primeira que realmente contém a
            # imagem (antes ele devolvia a primeira e o candidato era rejeitado
            # mesmo havendo uma segunda página válida).
            try:
                resolvido = resolver(
                    cand,
                    subject,
                    verifier=lambda c: validate_discovered_candidate(
                        c, cache=cache_paginas, cache_html=cache_html
                    ),
                )
            except Exception:  # noqa: BLE001 - resolver é best-effort
                resolvido = cand
            if str(resolvido.get("source_page_url") or "").strip():
                resolvido["usable"] = True
                resolvido["discovery_only"] = False
                resolvido.pop("rejected_reason", None)
                cand.clear()
                cand.update(resolvido)
            # O memo fica sob a chave ORIGINAL (sem origem): é a chave que o
            # próximo enriquecimento do MESMO candidato vai usar.
            _guardar_memo(cand, chave_original)
        _classificar(cand)
        if chave_original:
            # ALIAS da chave original, agora com o resultado COMPLETO
            # (evidence/evidence_score/phash): a gravação acima acontecia ANTES da
            # classificação e guardava uma entrada sem `evidence.verdict` — se o
            # mesmo candidato bruto reaparecesse sem origem, o memo incompleto
            # fazia o pipeline pular o processamento e o candidato terminava
            # rejeitado por "não verificado".
            _guardar_memo(cand, chave_original)

    # Candidatos que vieram do memo (não reprocessados) entram no resultado pela
    # MESMA decisão registrada — o memo é o resultado, não um atalho.
    for cand in candidates:
        if id(cand) in pendentes_ids:
            continue
        verdict = str((cand.get("evidence") or {}).get("verdict") or "")
        if cand.get("capacity_deferred"):
            deferidos.append(cand)
        elif verdict in ("deterministic_match", "ambiguous"):
            aprovados.append(cand)
        else:
            rejeitados.append(cand)
    # Fase 13 / ordem da busca: o que JÁ está na biblioteca vem antes de custo
    # novo (imagem validada e hospedada não precisa ser baixada nem enviada de
    # novo). Depois domínio oficial, depois score de evidência.
    if root is not None:
        try:
            from .media.library_index import find_by_source_url, find_similar

            for cand in aprovados:
                url_cand = str(cand.get("direct_image_url") or "")
                ja = find_by_source_url(root, url_cand) or (
                    find_similar(root, str(cand.get("phash") or ""))
                    if cand.get("phash")
                    else None
                )
                cand["already_in_library"] = bool(ja)
                if ja and ja.get("media_id"):
                    cand["library_media_id"] = ja.get("media_id")
        except Exception:  # noqa: BLE001 - índice é otimização
            pass

    aprovados.sort(
        key=lambda c: (
            bool(c.get("already_in_library")),
            bool(c.get("official_source")),
            c["evidence_score"],
        ),
        reverse=True,
    )

    # Fase 12: pHash ANTES da seleção/upload. Chamado SEMPRE (inclusive com um
    # único aprovado): a contagem de capacidade usa pHash global entre engines e
    # sem hash ela cai para URL — o mesmo frame servido por 2 engines (URLs
    # diferentes) contaria como 2 frames distintos.
    from .media.evidence import dedupe_by_phash

    aprovados, rejeitados = dedupe_by_phash(aprovados, rejeitados, hashes=hashes_locais)

    # Funil de yield POR ENGINE (documento, seção 15): o indicador de sucesso não
    # é "quantas imagens o Bing devolveu", e sim quantas atravessaram
    # descoberta -> origem verificada -> relevância. Com isso o agente passa a
    # escolher provider por yield medido, não por preferência fixa.
    if root is not None:
        try:
            from .observability import append_telemetry

            funil: dict[str, dict[str, int]] = {}
            for cand in aprovados + rejeitados:
                eng = str(cand.get("engine") or "unknown")
                linha = funil.setdefault(
                    eng, {"discovered": 0, "verified": 0, "subject_match": 0, "selected": 0,
                          "deferred": 0},
                )
                linha["discovered"] += 1
                if cand.get("valid"):
                    linha["verified"] += 1
                if (cand.get("evidence") or {}).get("verdict") == "deterministic_match":
                    linha["subject_match"] += 1
            for cand in deferidos:
                # Não investigado != reprovado: o funil conta em campo próprio.
                eng = str(cand.get("engine") or "unknown")
                funil.setdefault(
                    eng, {"discovered": 0, "verified": 0, "subject_match": 0, "selected": 0,
                          "deferred": 0},
                )["deferred"] += 1
            for cand in aprovados:
                funil[str(cand.get("engine") or "unknown")]["selected"] += 1
            for eng, linha in funil.items():
                append_telemetry(root, "media_engine_yield", engine=eng, **linha)
        except Exception:  # noqa: BLE001 - telemetria nunca quebra a busca
            pass
    return aprovados, rejeitados, deferidos


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        build_parser().print_help()
        return 0
    try:
        if args.command == "maintenance-report":
            data = json.loads(args.report_file.read_text(encoding="utf-8"))
            posts = data.get("posts", []) if isinstance(data, dict) else data
            result = generate_report(
                posts,
                broken_urls=args.broken_url,
                min_inline_images=args.min_inline_images,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        config = load_config()
        if args.command == "apply" and getattr(args, "dry_run", False):
            config = replace(config, dry_run=True)
        client = WordPressClient(config)
        if args.command == "list-pending":
            result = client.list_pending(page=args.page, per_page=config.batch_limit)
            if args.compact:
                result = _compact_listing(result)
        elif args.command == "telemetry":
            if getattr(args, "sessions", False):
                # Por post e por sessao: o ledger do pipeline x o gasto real da
                # sessao Hermes (state.db, somente leitura).
                from .session_metrics import session_metrics

                result = session_metrics(
                    args.root,
                    state_db=getattr(args, "state_db", None),
                    job_id=(
                        getattr(args, "job_id", "")
                        or os.environ.get("HERMES_EDITORIAL_CRON_JOB_ID", "")
                    ),
                    project_root=getattr(args, "project_root", "") or str(args.root),
                    hours=int(getattr(args, "hours", 24) or 24),
                )
            else:
                from .observability import read_telemetry_summary

                result = read_telemetry_summary(
                    args.root,
                    batch_id=(getattr(args, "batch_id", "") or None),
                )
        elif args.command == "canary-preflight":
            result = _canary_preflight(client, config, args.root, list(args.post_ids))
        elif args.command == "migrate-state":
            from .workflow import migrate_legacy_state

            result = migrate_legacy_state(
                client, config, args.root,
                apply=bool(getattr(args, "apply", False)),
                limit=int(getattr(args, "limit", 0) or 0),
            )
        elif args.command == "reconcile":
            from .reconcile import reconcile_state

            statuses = tuple(
                s.strip() for s in str(getattr(args, "statuses", "") or "").split(",") if s.strip()
            )
            result = reconcile_state(
                client,
                config,
                args.root,
                statuses=statuses or ("pending", "awaiting_human"),
                limit=int(getattr(args, "limit", 100) or 100),
            )
        elif args.command == "queue":
            report = build_queue_report(client, args.root)
            if args.monitor:
                # Linha estavel hasheada pelo cron do Hermes (--monitor-script):
                # so muda quando ha trabalho elegivel real, nunca a cada tick.
                print(_monitor_line(report))
                return 0
            result = _compact_queue(report) if args.compact else report
        elif args.command == "cards":
            from . import session_budget

            # HARD STOP de sessao: orcamento de CONTEXTO estourado (ou teto de
            # posts tocados esgotado) encerra a sessao ANTES de montar qualquer
            # card — a sessao nao pode "gastar so mais um pouquinho". Sem isso era
            # possivel o estado remaining_posts=1 + context_budget_exceeded=true
            # ainda devolver outro card, contrariando a propria documentacao.
            orcamento = session_budget.status(args.root, config)
            motivo = session_budget.stop_reason(args.root, config)
            per_page = config.max_posts_per_run if args.limit is None else int(args.limit)
            if orcamento["remaining_posts"] is not None:
                per_page = min(per_page, int(orcamento["remaining_posts"]))
            if motivo:
                result = {"count": 0, "cards": [], "stop": motivo}
                if args.compact:
                    _write_audit(args.root / "work" / "cards.latest.json", result)
            else:
                result = (
                    build_cards(client, config, args.root, per_page=per_page)
                    if per_page > 0
                    else {"count": 0, "cards": []}
                )
                if args.compact:
                    # Auditoria completa em arquivo; terminal so com a acao por post.
                    _write_audit(args.root / "work" / "cards.latest.json", result)
                    result = _compact_cards(result)
            result["session"] = session_budget.status(args.root, config)
        elif args.command == "prepare":
            result = prepare_post(client, args.root, args.post_id)
            if args.compact:
                prepared_file = args.root / "backups" / str(args.post_id) / "prepared.json"
                prepared_file.parent.mkdir(parents=True, exist_ok=True)
                prepared_file.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                result = {
                    "post_id": result["post_id"],
                    "status": result["status"],
                    "backup": result["backup"],
                    "prepared": str(prepared_file),
                    "word_count": _html_word_count(result.get("cleaned_html", "")),
                    "original_link": result.get("original_link"),
                    "wordpress_changed": False,
                }
        elif args.command == "prepare-batch":
            result = prepare_batch(
                client,
                config,
                args.root,
                args.post_ids,
                batch_id=args.batch_id or None,
            )
        elif args.command == "editorial-generate-batch":
            from .editorial_provider import generate_editorial_batch

            batch_id = ""
            try:
                batch_id = str(json.loads(args.input_file.read_text(encoding="utf-8")).get("batch_id") or "")
            except (OSError, ValueError):
                pass
            previous_batch = os.environ.get("UNICORNIO_BATCH_ID")
            previous_stage = os.environ.get("UNICORNIO_BATCH_STAGE")
            if batch_id:
                os.environ["UNICORNIO_BATCH_ID"] = batch_id
            os.environ["UNICORNIO_BATCH_STAGE"] = "editorial"
            try:
                result = generate_editorial_batch(
                    args.input_file,
                    api_key=config.editorial_api_key,
                    base_url=config.editorial_base_url,
                    model=config.editorial_model,
                    timeout=max(config.http_timeout, 30.0),
                    min_confidence=config.min_relevance_confidence,
                    root=args.root,
                    output_path=args.output,
                )
            finally:
                if previous_batch is None:
                    os.environ.pop("UNICORNIO_BATCH_ID", None)
                else:
                    os.environ["UNICORNIO_BATCH_ID"] = previous_batch
                if previous_stage is None:
                    os.environ.pop("UNICORNIO_BATCH_STAGE", None)
                else:
                    os.environ["UNICORNIO_BATCH_STAGE"] = previous_stage
        elif args.command == "apply-batch":
            batch = load_editorial_batch(args.batch_file)
            if args.dry_run:
                config = replace(config, dry_run=True)
            result = _apply_editorial_batch(
                client,
                config,
                args.root,
                batch,
                dry_run=bool(args.dry_run),
                compact=bool(args.compact),
            )
        elif args.command == "checklist":
            payload = json.loads(args.editorial_file.read_text(encoding="utf-8"))
            editorial = validate_editorial(payload, min_confidence=config.min_relevance_confidence)
            post = client.get_post(args.post_id)
            if editorial["site_relevance"]["decision"] == "process":
                editorial = resolve_editorial_defaults(editorial, post)
            backup = SnapshotStore(args.root).save(args.post_id, post)
            content, trailer, trailer_status = compose_final_content(
                editorial, config, original_link_of(post), root=args.root
            )
            editorial = attach_trailer_audit(
                editorial, trailer, search_status=trailer_status
            )
            result = run_pre_publish_checklist(
                post=post,
                editorial=editorial,
                content=content,
                backup_path=backup,
                config=config,
                client=client,
            )
            result["trailer"] = trailer
            if args.compact:
                result = _compact_checklist(result)
        elif args.command == "publish":
            result = publish_post(client, config, args.root, args.post_id)
        elif args.command == "media-search":
            items = client.search_media(args.termo, per_page=args.limit)
            result = [_media_search_item(item) for item in items]
        elif args.command == "media-similar":
            from .media.visual_hash import similar_image_pairs

            pares = similar_image_pairs(args.urls, threshold=args.threshold)
            result = {
                "threshold": args.threshold,
                "imagens_comparadas": len(args.urls),
                "pares_mesmo_frame": [
                    {"a": a, "b": b, "distancia": d} for a, b, d in pares
                ],
                "veredito": "REPETIDAS" if pares else "todas distintas",
            }
        elif args.command == "media-search-web":
            from .media.search import search_web_images
            from .observability import append_telemetry

            # P0/P1 (auditoria de contexto):
            #  * o subject é resolvido ANTES da busca (o termo digitado é só a query);
            #  * a Media Library/índice local é consultada ANTES da web: imagem já
            #    validada é REUSO (proveniência registrada), e só o déficit
            #    restante vai para os engines;
            #  * a busca é ADAPTATIVA por déficit: ``--needed N`` (o "faltam N
            #    imagens" do card) é a meta de CAPACIDADE — encerra assim que
            #    houver N candidatos FORTES distintos e só expande entre engines
            #    enquanto faltar capacidade (nunca mais 10 fixos);
            #  * o stdout é COMPACTO (só o que o agente pode usar); o JSON
            #    completo vai para work/search/<chave>.json (auditoria).
            subject_alvo = args.termo
            post_id_ref = int(getattr(args, "post_id", 0) or 0)
            if post_id_ref:
                try:
                    from .media.evidence import post_subjects

                    post_ref = client.get_post(post_id_ref)
                    titulo_ref = post_ref.get("title")
                    conteudo_ref = post_ref.get("content") or {}
                    subs = post_subjects(
                        title=(titulo_ref or {}).get("raw", "") if isinstance(titulo_ref, dict) else str(titulo_ref or ""),
                        content_html=(conteudo_ref.get("raw") or "") if isinstance(conteudo_ref, dict) else "",
                    )
                    if subs:
                        subject_alvo = str(subs[0]["subject"])
                except Exception:  # noqa: BLE001 - sem post, segue com o termo
                    pass

            needed = int(getattr(args, "needed", 0) or 0)
            if needed <= 0:
                needed = max(1, int(getattr(args, "limit", 10) or 1))

            # Media Library/índice local PRIMEIRO (P1): só o que falta vai à web.
            reuso = _reuse_from_library(client, args.root, subject_alvo, limit=needed)
            needed_web = max(0, needed - len(reuso))

            memo: dict = {}
            _frames_fortes: set[str] = set()
            _frames_ambiguos: set[str] = set()

            def _avaliar_capacidade(novos: list[dict]) -> int:
                """Frames FORTES distintos já aceitos (o que encerra a busca).

                Ambíguo NÃO encerra a busca: quando os candidatos são fracos ou
                ambíguos o pipeline EXPANDE para as outras engines (a visão
                continua disponível para o caso ambíguo — nunca para cortar
                custo às custas do gate).
                """
                try:
                    aprovados_lote, _rej, _def = _enriquecer_candidatos(
                        novos,
                        subject=subject_alvo,
                        termo=args.termo,
                        verify=bool(getattr(args, "verify", True)),
                        capacity=needed_web,
                        enriched_cache=memo,
                    )
                except Exception:  # noqa: BLE001 - aceite nunca derruba a busca
                    return len(_frames_fortes)
                for aprovado in aprovados_lote:
                    chave_frame = str(
                        aprovado.get("phash") or aprovado.get("direct_image_url") or ""
                    )
                    if (aprovado.get("evidence") or {}).get("verdict") == "deterministic_match":
                        _frames_fortes.add(chave_frame)
                    else:
                        _frames_ambiguos.add(chave_frame)
                return len(_frames_fortes)

            candidates: list[dict] = []
            if needed_web > 0:
                candidates = search_web_images(
                    args.termo,
                    size=args.size,
                    ratio=args.ratio,
                    # Capacidade + 1 folga por engine: a PARADA é a capacidade
                    # aceita (frames fortes), não uma cota fixa de 10 candidatos.
                    limit=needed_web + 1,
                    engine=getattr(args, "engine", "auto"),
                    accept=_avaliar_capacidade,
                )
            engines_queried = sorted(
                {str(c.get("engine") or "") for c in candidates if c.get("engine")}
            )
            if needed_web > 0 and not candidates:
                # Possivel bloqueio/rate-limit do Google em producao: registrar
                # para o operador distinguir "nao ha imagem" de "busca falhou".
                append_telemetry(
                    args.root, "media_search_empty",
                    query=args.termo,
                    size_filter=f"{args.size}|{args.ratio}",
                )
            else:
                for candidate in candidates:
                    direct_url = str(candidate.get("direct_image_url") or "")
                    append_telemetry(
                        args.root,
                        "media_funnel",
                        stage="discovery",
                        status="passed",
                        source_domain=(urlparse(direct_url).hostname or "").lower(),
                        engine=str(candidate.get("engine") or "unknown"),
                    )
            aprovados: list[dict] = []
            rejeitados: list[dict] = []
            deferidos: list[dict] = []
            if candidates:
                # MESMO enriquecimento do callback, agora reaproveitado pelo memo
                # (P1): sem ele cada candidato era resolvido/pontuado/hasheado 2x.
                aprovados, rejeitados, deferidos = _enriquecer_candidatos(
                    candidates,
                    subject=subject_alvo,
                    termo=args.termo,
                    verify=bool(getattr(args, "verify", True)),
                    root=args.root,
                    capacity=needed_web,
                    enriched_cache=memo,
                )
            if rejeitados:
                append_telemetry(
                    args.root, "media_source_rejections",
                    query=args.termo, rejected=len(rejeitados),
                    approved=len(aprovados),
                )
            decisao_auditoria = _final_decision(
                aprovados, reuso, needed, margin=int(config.auto_score_margin)
            )
            decision_id = ""
            if post_id_ref:
                # Ledger (append-only) da decisão: os gates seguintes
                # (media-validate, apply) carregam esta decisão nos eventos, o que
                # permite medir se `auto`/`reuse` pioraram a qualidade. O id viaja
                # no stdout/plano para rastrear a decisão de CADA imagem.
                try:
                    from .observability import record_media_decision

                    selecionado = decisao_auditoria.get("select") or {}
                    decision_id = record_media_decision(
                        args.root, post_id_ref,
                        decision=str(decisao_auditoria["decision"]),
                        score_gap=decisao_auditoria.get("score_gap"),
                        query=args.termo,
                        subject=subject_alvo,
                        selected_url=str(selecionado.get("url") or ""),
                        coverage=str(decisao_auditoria.get("coverage") or ""),
                    )
                except Exception:  # noqa: BLE001 - ledger é instrumentação
                    decision_id = ""
            # Economia de mídia medível (P0/P1): quanto veio do acervo local,
            # quantas buscas web de fato aconteceram, quantos candidatos foram
            # examinados e quantos foram apenas DISPENSADOS por capacidade
            # (deferidos != rejeitados).
            append_telemetry(
                args.root, "media_search_result",
                query=args.termo,
                post_id=post_id_ref or 0,
                needed=needed,
                needed_web=needed_web,
                reuse=len(reuso),
                strong=sum(
                    1 for c in aprovados
                    if (c.get("evidence") or {}).get("verdict") == "deterministic_match"
                ),
                ambiguous=len(_frames_ambiguos),
                accepted=len(aprovados),
                rejected=len(rejeitados),
                deferred=len(deferidos),
                examined=len(candidates),
                engines_queried=len(engines_queried),
                decision=decisao_auditoria["decision"],
                decision_reason=str(decisao_auditoria.get("reason") or "")[:200],
                decision_id=decision_id,
                coverage=str(decisao_auditoria.get("coverage") or ""),
            )
            audit = _write_audit(
                args.root / "work" / "search" / f"{_audit_key(args.termo, post_id_ref)}.json",
                {
                    "query": args.termo,
                    "size_filter": f"{args.size}|{args.ratio}",
                    "subject": subject_alvo,
                    "needed": needed,
                    "needed_web": needed_web,
                    "reuse": reuso,
                    "engines_queried": engines_queried,
                    "count": len(aprovados),
                    "candidates": aprovados,
                    "rejected": rejeitados,
                    "rejected_count": len(rejeitados),
                    "deferred": deferidos,
                    "deferred_count": len(deferidos),
                    # Decisão + RAZÃO + scores no artefato: é assim que se
                    # verifica depois se a economia de visão/julgamento reduziu
                    # a qualidade das imagens escolhidas.
                    "decision": {
                        "kind": decisao_auditoria["decision"],
                        "reason": decisao_auditoria.get("reason"),
                        "selected": decisao_auditoria.get("select"),
                        "options": decisao_auditoria.get("options") or [],
                    },
                },
            )
            if getattr(args, "compact", True):
                result = _compact_media_search(
                    query=args.termo,
                    subject=subject_alvo,
                    needed=needed,
                    reuso=reuso,
                    aprovados=aprovados,
                    rejeitados=rejeitados,
                    engines=engines_queried,
                    audit=audit,
                    ambiguous=len(_frames_ambiguos),
                    deferred=len(deferidos),
                    margin=int(config.auto_score_margin),
                )
            else:
                result = {
                    "query": args.termo,
                    "size_filter": f"{args.size}|{args.ratio}",
                    "subject": subject_alvo,
                    "needed": needed,
                    "reuse": reuso,
                    "engines_queried": engines_queried,
                    "count": len(aprovados),
                    "candidates": aprovados,
                    "rejected": rejeitados,
                    "rejected_count": len(rejeitados),
                    "deferred": deferidos,
                    "deferred_count": len(deferidos),
                    "decision": {
                        "kind": decisao_auditoria["decision"],
                        "reason": decisao_auditoria.get("reason"),
                        "selected": decisao_auditoria.get("select"),
                        "options": decisao_auditoria.get("options") or [],
                    },
                    "audit": audit,
                }
        elif args.command == "media-search-listicle":
            from .media.search import search_web_images_batch
            from .observability import append_telemetry

            # Fase 8: a QUERY de cada item ganha o tipo de conteudo do artigo
            # ("Pluto" -> "Pluto anime"), enquanto o SUBJECT continua sendo o
            # item original — a busca fica no dominio certo e o score mede a
            # evidencia contra o que o item realmente nomeia.
            from .media.evidence import item_query

            artigo_titulo = str(getattr(args, "article_title", "") or "")
            queries_listicle = [item_query(t, artigo_titulo) or t for t in args.titulos]
            subject_por_query = {q: t for q, t in zip(queries_listicle, args.titulos)}
            # P1 (auditoria): o listicle TAMBÉM encerra a busca por ACEITOS. Sem o
            # callback o item caía no critério antigo ("usable") e uma engine com
            # candidatos estruturalmente válidos mas ruins encerrava a pesquisa
            # daquele item — Google/Yandex nunca eram consultados.
            _frames_por_item: dict[str, set[str]] = {}
            # Memo por (url+origem+subject) compartilhado entre o callback de
            # aceite e o enriquecimento final (mesmo ganho do media-search-web:
            # nada era processado duas vezes).
            _memo_listicle: dict = {}

            def _aceitar_item(novos: list[dict], query: str = "") -> int:
                item = str(query or "")
                try:
                    aprovados_lote, _rej, _def = _enriquecer_candidatos(
                        novos,
                        subject=str(subject_por_query.get(item) or item),
                        termo=item,
                        root=args.root,
                        enriched_cache=_memo_listicle,
                    )
                except Exception:  # noqa: BLE001 - aceite nunca derruba a busca
                    return 0
                conjunto = _frames_por_item.setdefault(item, set())
                for aprovado in aprovados_lote:
                    conjunto.add(
                        str(aprovado.get("phash") or aprovado.get("direct_image_url") or "")
                    )
                return len(conjunto)

            rows = search_web_images_batch(
                queries_listicle,
                size=args.size,
                ratio=args.ratio,
                limit=args.limit,
                engine=args.engine,
                accept=_aceitar_item,
            )
            items = []
            audit_items: list[dict] = []
            rejeitados_por_query: dict[str, list[dict]] = {}
            deferidos_por_query: dict[str, list[dict]] = {}
            missing: list[str] = []
            for row in rows:
                query = str(row["query"])
                candidates = list(row.get("candidates") or [])
                decisao_item: dict = {}
                decision_id_item = ""
                if not candidates:
                    missing.append(query)
                    append_telemetry(
                        args.root, "media_search_empty",
                        query=query, size_filter=f"{args.size}|{args.ratio}", batch=True,
                    )
                else:
                    # Listicle: cada item/H2 roda o MESMO pipeline do artigo —
                    # origem verificada + contexto da página + score DELE.
                    # Uma imagem do item "Pluto" nunca é aceita por evidência do
                    # item "Cyberpunk".
                    subject_item = str(subject_por_query.get(query) or query)
                    candidates, rejeitados_item, deferidos_item = _enriquecer_candidatos(
                        candidates, subject=subject_item, termo=query, root=args.root,
                        enriched_cache=_memo_listicle,
                        # Cada ITEM precisa de UMA imagem: capacidade 1 pelo mesmo
                        # critério do artigo (forte + frame distinto), evitando
                        # investigar candidato que já não é necessário.
                        capacity=1,
                    )
                    rejeitados_por_query[query] = rejeitados_item
                    deferidos_por_query[query] = deferidos_item
                    # Decisão POR ITEM (append-only): a lista passa a ter
                    # decisão_id por item, então o media-validate consegue dizer
                    # qual imagem do item 3 foi rejeitada e qual decisão a
                    # escolheu (`auto` com gap X, `choose`, ...).
                    decisao_item = _final_decision(
                        candidates, [], 1, margin=int(config.auto_score_margin)
                    )
                    decision_id_item = ""
                    try:
                        from .observability import record_media_decision

                        selecionado_item = decisao_item.get("select") or {}
                        decision_id_item = record_media_decision(
                            args.root,
                            int(getattr(args, "post_id", 0) or 0),
                            decision=str(decisao_item["decision"]),
                            score_gap=decisao_item.get("score_gap"),
                            query=query,
                            subject=subject_item,
                            item_index=len(audit_items),
                            selected_url=str(selecionado_item.get("url") or ""),
                            coverage=str(decisao_item.get("coverage") or ""),
                        )
                    except Exception:  # noqa: BLE001 - ledger é instrumentação
                        decision_id_item = ""
                    for candidate in candidates:
                        direct_url = str(candidate.get("direct_image_url") or "")
                        append_telemetry(
                            args.root, "media_funnel", stage="discovery", status="passed",
                            source_domain=(urlparse(direct_url).hostname or "").lower(),
                            engine=str(candidate.get("engine") or "unknown"), batch=True,
                        )
                    # Economia de mídia por ITEM do listicle (mesma unidade do
                    # artigo): examinados, fortes, ambíguos e DISPENSADOS por
                    # capacidade (deferido != rejeitado). Em lote o número de
                    # engines é um proxy (1 = o item teve candidato): a contagem
                    # exata por item não existe no batch.
                    append_telemetry(
                        args.root, "media_search_result",
                        query=query, post_id=int(getattr(args, "post_id", 0) or 0),
                        item_index=len(audit_items), batch=True,
                        needed=1, needed_web=1, reuse=0,
                        strong=sum(
                            1 for c in candidates
                            if (c.get("evidence") or {}).get("verdict") == "deterministic_match"
                        ),
                        ambiguous=sum(
                            1 for c in candidates
                            if (c.get("evidence") or {}).get("verdict") == "ambiguous"
                        ),
                        accepted=len(candidates),
                        rejected=len(rejeitados_item),
                        deferred=len(deferidos_item),
                        examined=len(candidates),
                        engines_queried=1 if candidates else 0,
                        decision=str(decisao_item["decision"]),
                        decision_reason=str(decisao_item.get("reason") or "")[:200],
                        decision_id=decision_id_item,
                        coverage=str(decisao_item.get("coverage") or ""),
                    )
                    if not candidates:
                        missing.append(query)
                # O browser visual não precisa de thumbnail/size; o JSON
                # editorial agora leva o veredito de evidência (subject, score,
                # verdict) para o agente escolher sem "adivinhar" relevância.
                audit_items.append(
                    {"query": query, "candidates": candidates,
                     "rejected": rejeitados_por_query.get(query, []),
                     "deferred": deferidos_por_query.get(query, [])}
                )
                compact = [
                    {
                        "query": candidate.get("query", ""),
                        "engine": candidate.get("engine", ""),
                        "title": candidate.get("title", ""),
                        "direct_image_url": candidate.get("direct_image_url", ""),
                        "source_page_url": candidate.get("source_page_url", ""),
                        "subject": candidate.get("subject", ""),
                        "evidence_score": candidate.get("evidence_score", 0),
                        "verdict": (candidate.get("evidence") or {}).get("verdict", ""),
                        "needs_vision": candidate.get("needs_vision", False),
                    }
                    for candidate in candidates
                ]
                items.append({
                    "query": query,
                    "count": len(compact),
                    # Rastreabilidade POR ITEM: o agente copia `decision_id` para
                    # cada entrada do media_plan, então o media-validate/apply
                    # atribuem o resultado à decisão DAQUELE item (antes tudo caía
                    # na "última decisão do post").
                    "decision_id": decision_id_item,
                    "decision": str(decisao_item.get("decision") or ""),
                    "coverage": str(decisao_item.get("coverage") or ""),
                    "select": decisao_item.get("select"),
                    "options": decisao_item.get("options") or [],
                    "candidates": compact,
                })
            audit = _write_audit(
                args.root / "work" / "search" / f"listicle-{_audit_key('-'.join(queries_listicle))}.json",
                {
                    "article_title": artigo_titulo,
                    "size_filter": f"{args.size}|{args.ratio}",
                    "items": audit_items,
                },
            )
            result = {
                "requested": len(rows),
                "found": len(rows) - len(missing),
                "missing_queries": missing,
                "rejected_summary": {
                    query: _rejected_summary(rejeitados)
                    for query, rejeitados in rejeitados_por_query.items()
                    if rejeitados
                },
                "deferred_summary": {
                    query: len(deferidos)
                    for query, deferidos in deferidos_por_query.items()
                    if deferidos
                },
                "audit": audit,
                "items": items,
            }
        elif args.command == "content":
            # P2 da auditoria de contexto: `requires_content=false` precisa
            # valer ate o FIM do fluxo, nao so como conselho no card. Se o gate
            # que bloqueou o post nao pediu reescrita (midia/SEO/trailer), o
            # corpo nao entra na conversa: o comando explica o que fazer em vez
            # de despejar o artigo. `--force` existe para quando o agente DECIDE
            # reescrever o texto; post novo (sem gate de rework) segue liberado.
            componentes = []
            if not getattr(args, "force", False):
                componentes = _blocked_components(args.root, args.post_id)
            if componentes and "text" not in componentes:
                from .state import read_state

                bloqueado = False
                try:
                    bloqueado = read_state(client.get_post(args.post_id))["state"] == STATE_BLOCKED
                except Exception:  # noqa: BLE001 - sem estado confiavel, nao bloqueia
                    bloqueado = False
                if bloqueado:
                    result = {
                        "post_id": args.post_id,
                        "status": "content_not_required",
                        "component": componentes,
                        "reason": (
                            "o fix deste post e "
                            + ", ".join(componentes)
                            + ": o corpo do artigo nao e necessario"
                        ),
                        "action": (
                            "use `draft POST_ID --for-fix` e corrija SO o componente "
                            "(apply ... --merge-draft). Para reescrever o texto de "
                            "verdade, repita com --force"
                        ),
                    }
                else:
                    result = get_cleaned_content(client, args.root, args.post_id)
            else:
                result = get_cleaned_content(client, args.root, args.post_id)
        elif args.command == "media-validate":
            payload = json.loads(args.editorial_file.read_text(encoding="utf-8"))
            post_title = ""
            existing_featured_id = None
            if args.post_id:
                post = client.get_post(args.post_id)
                title_value = post.get("title") or {}
                post_title = str(
                    title_value.get("raw") or title_value.get("rendered") or ""
                )
                existing_featured_id = int(post.get("featured_media") or 0) or None
            result = validate_media_plan(
                client,
                payload,
                config=config,
                root=args.root,
                post_title=post_title,
                existing_featured_id=existing_featured_id,
            )
            # Regra do repo: detalhe completo em arquivo, stdout pequeno.
            audit = _write_audit(
                args.root / "work" / "media-validate"
                / f"{args.post_id or _audit_key(args.editorial_file.name)}.json",
                result,
            )
            # Evento de QUALIDADE. O dado AUTORITATIVO é o `decision_id`: o
            # `decision`/`score_gap` do item vêm do LEDGER, nunca do JSON do
            # agente (que pode copiar errado). Sem isso, o item AAA podia sair
            # rotulado com o `score_gap` da ÚLTIMA decisão do post. Cada item do
            # plano emite seu evento com o estado da atribuição
            # (resolved/missing/invalid); o agregado do post sai SEM rótulo de
            # decisão para não duplicar em `decision_quality`.
            try:
                from .observability import append_telemetry, attribution_of, read_media_decision_by_id

                plano = payload.get("media_plan") or []
                rejeitados_idx = {
                    int(row.get("index"))
                    for row in (result.get("rejected") or [])
                    if isinstance(row.get("index"), int)
                }
                ids_plano: list[str] = []
                estados: list[str] = []
                rotulos: list[str] = []
                for indice, item in enumerate(plano):
                    if not isinstance(item, dict):
                        continue
                    identificador = str(item.get("decision_id") or "")
                    estado = attribution_of(args.root, args.post_id or 0, identificador)
                    estados.append(estado)
                    # `decision`/`score_gap` SÓ do ledger; o texto do plano é
                    # ignorado para medição (fica no JSON como documentação).
                    do_ledger = (
                        read_media_decision_by_id(args.root, args.post_id or 0, identificador)
                        if estado == "resolved" else {}
                    )
                    if estado == "resolved" and str(do_ledger.get("decision") or ""):
                        rotulos.append(str(do_ledger["decision"]))
                    if identificador:
                        ids_plano.append(identificador)
                    append_telemetry(
                        args.root, "media_validate_result",
                        post_id=args.post_id or 0,
                        item_index=indice,
                        valid=indice not in rejeitados_idx,
                        rejected_items=1 if indice in rejeitados_idx else 0,
                        attribution=estado,
                        decision=str(do_ledger.get("decision") or ""),
                        decision_id=identificador,
                        score_gap=do_ledger.get("score_gap"),
                    )
                # Agregado do post: atribuição em PIOR CASO entre os itens
                # (invalid > missing > resolved) e `decision_scope` dizendo se o
                # plano é uniforme ou misto. `decision` fica SEMPRE vazio aqui —
                # o rótulo pertence ao item, nunca ao post.
                if not estados:
                    agregado_estado = "missing"
                elif "invalid" in estados:
                    agregado_estado = "invalid"
                elif "missing" in estados:
                    agregado_estado = "missing"
                else:
                    agregado_estado = "resolved"
                # `decision_scope` usa os RÓTULOS RESOLVIDOS no ledger (não os
                # ids): `auto + auto` é plano UNIFORME, mesmo com dois ids
                # diferentes — a versão por ids dizia "mixed" e divergia do apply.
                # Com qualquer item missing/invalid a decisão de todos não é
                # conhecida: sem scope (não se declara uniform nem mixed).
                if agregado_estado != "resolved":
                    escopo = ""
                elif len({r for r in rotulos if r}) > 1:
                    escopo = "mixed"
                else:
                    escopo = "uniform"
                append_telemetry(
                    args.root, "media_validate_result",
                    post_id=args.post_id or 0,
                    valid=result.get("valid"),
                    rejected_items=len(result.get("rejected") or []),
                    featured_status=str(
                        ((result.get("featured_vision") or [{}])[0] or {}).get("status") or ""
                    ),
                    attribution=agregado_estado,
                    decision_scope=escopo,
                    decision="",
                    score_gap=None,
                    decision_id="",
                    decision_ids=ids_plano,
                )
            except Exception:  # noqa: BLE001 - telemetria nunca derruba o CLI
                pass
            if getattr(args, "compact", True):
                result = _compact_media_validate(
                    payload, result, audit=audit, post_title=post_title
                )
        elif args.command == "vision-batch":
            from .media.vision_cache import set_cached_decision
            from .media.vision_gate import verify_image_subject_batch

            batch = load_vision_batch(args.batch_file)
            previous_batch = os.environ.get("UNICORNIO_BATCH_ID")
            previous_stage = os.environ.get("UNICORNIO_BATCH_STAGE")
            os.environ["UNICORNIO_BATCH_ID"] = batch["batch_id"]
            os.environ["UNICORNIO_BATCH_STAGE"] = "vision"
            try:
                decisions = verify_image_subject_batch(
                    items=batch["items"],
                    api_key=config.vision_api_key,
                    base_url=config.vision_base_url,
                    model=config.vision_model,
                    timeout=config.http_timeout,
                    detail=config.vision_detail,
                    allow_high=True,
                    root=args.root,
                )
            finally:
                if previous_batch is None:
                    os.environ.pop("UNICORNIO_BATCH_ID", None)
                else:
                    os.environ["UNICORNIO_BATCH_ID"] = previous_batch
                if previous_stage is None:
                    os.environ.pop("UNICORNIO_BATCH_STAGE", None)
                else:
                    os.environ["UNICORNIO_BATCH_STAGE"] = previous_stage
            items: list[dict[str, Any]] = []
            for item in batch["items"]:
                candidate_id = item["candidate_id"]
                decision = decisions[candidate_id]
                cached = False
                # Only definitive final results enter the normal cache. The
                # batch gate already escalated only the ambiguous subset to
                # high detail; inconclusive-at-high remains uncached.
                if decision.get("verdict") in {"accept", "reject"}:
                    set_cached_decision(
                        args.root,
                        item["image_url"],
                        item["subject"],
                        {
                            "status": "MATCH"
                            if decision["verdict"] == "accept"
                            else "UNRELATED",
                            "confidence": float(decision.get("confidence") or 0),
                            "visual_type": decision.get("visual_type") or "other",
                        },
                    )
                    cached = True
                items.append(
                    {
                        "candidate_id": candidate_id,
                        "post_id": item.get("post_id"),
                        "status": decision.get("status"),
                        "verdict": decision.get("verdict"),
                        "confidence": decision.get("confidence"),
                        "visual_type": decision.get("visual_type"),
                        "reason": decision.get("reason"),
                        "cached": cached,
                    }
                )
            full = {
                "schema_version": batch["schema_version"],
                "batch_id": batch["batch_id"],
                "items": items,
            }
            audit = _write_audit(
                args.root / "work" / "batches" / batch["batch_id"] / "vision.manifest.json",
                full,
            )
            result = {
                "schema_version": batch["schema_version"],
                "batch_id": batch["batch_id"],
                "count": len(items),
                "accepted": sum(1 for item in items if item.get("verdict") == "accept"),
                "rejected": sum(1 for item in items if item.get("verdict") == "reject"),
                "inconclusive": sum(
                    1 for item in items if item.get("verdict") == "inconclusive"
                ),
                "cached": sum(1 for item in items if item.get("cached")),
                "audit": audit,
            }
            if args.full:
                result["items"] = items
        elif args.command == "media-resolve-batch":
            batch = load_media_resolve_batch(args.batch_file)
            previous_batch = os.environ.get("UNICORNIO_BATCH_ID")
            previous_stage = os.environ.get("UNICORNIO_BATCH_STAGE")
            os.environ["UNICORNIO_BATCH_ID"] = batch["batch_id"]
            os.environ["UNICORNIO_BATCH_STAGE"] = "media-resolve"
            try:
                result = _resolve_media_batch(
                    client, config, args.root, batch, full=bool(args.full)
                )
            finally:
                if previous_batch is None:
                    os.environ.pop("UNICORNIO_BATCH_ID", None)
                else:
                    os.environ["UNICORNIO_BATCH_ID"] = previous_batch
                if previous_stage is None:
                    os.environ.pop("UNICORNIO_BATCH_STAGE", None)
                else:
                    os.environ["UNICORNIO_BATCH_STAGE"] = previous_stage
        elif args.command == "draft":
            draft = load_draft(args.root, args.post_id)
            componentes = [args.component] if getattr(args, "component", "") else []
            if componentes or getattr(args, "for_fix", False):
                # Rework: o agente recebe SO o componente apontado pelo fix (o
                # draft inteiro fica no arquivo) — nao reenvia o artigo ao LLM
                # para corrigir uma imagem.
                if not componentes:
                    componentes = _blocked_components(args.root, args.post_id)
                result = _draft_for_fix(args.root, args.post_id, draft, componentes)
            else:
                result = draft
        elif args.command == "retry-all":
            states = {st.strip() for st in (args.states or "").split(",") if st.strip()}
            report = build_queue_report(client, args.root)
            target_ids: list[int] = []
            for row in report.get("posts") or []:
                st = row.get("state")
                if st == STATE_AWAITING_HUMAN and "awaiting_human" in states:
                    target_ids.append(int(row["id"]))
                elif st == STATE_BLOCKED and "blocked" in states:
                    target_ids.append(int(row["id"]))
            outcomes = []
            for pid in sorted(target_ids):
                try:
                    outcomes.append(retry_post(client, config, args.root, pid))
                except Exception as exc:  # noqa: BLE001 - report per post
                    outcomes.append({"post_id": pid, "status": "error", "reason": str(exc)})
            result = {
                "retried": sum(1 for o in outcomes if o.get("status") == "retried"),
                "failed": sum(1 for o in outcomes if o.get("status") == "error"),
                "posts": [
                    {"post_id": o.get("post_id"), "status": o.get("status"),
                     "state": o.get("state"), "reason": o.get("reason")}
                    for o in outcomes
                ],
            }
        elif args.command == "retry":
            result = retry_post(client, config, args.root, args.post_id)
        elif args.command == "discard":
            result = discard_post(client, config, args.root, args.post_id, reason=args.reason)
        elif args.command == "uncertain":
            result = mark_uncertain(client, config, args.root, args.post_id, reason=args.reason)
        elif args.command == "publish-ready":
            outcomes = publish_ready_posts(client, config, args.root, limit=config.publish_limit)
            published = [o for o in outcomes if o.get("wordpress_changed")]
            blocked = [o for o in outcomes if o.get("status") in ("blocked", "error")]
            if published or blocked:
                result = {
                    "published": len(published),
                    "posts": [
                        {
                            "post_id": o.get("post_id"),
                            "link": o.get("link"),
                            "published_at": o.get("published_at"),
                        }
                        for o in published
                    ],
                    "blocked_or_skipped": len(outcomes) - len(published),
                    "quality_blocked": len(blocked),
                    "blocked_posts": [
                        {
                            "post_id": outcome.get("post_id"),
                            "status": outcome.get("status"),
                            "reason": outcome.get("reason"),
                            "failed": [
                                item.get("name")
                                for item in ((outcome.get("checklist") or {}).get("items") or [])
                                if item.get("status") in ("fail", "error")
                            ],
                        }
                        for outcome in blocked
                    ],
                }
            else:
                # Watchdog pattern: silent when nothing was published and no
                # quality gate fired (everything cleanly skipped).
                return 0
        else:
            from . import session_budget

            payload = json.loads(args.editorial_file.read_text(encoding="utf-8"))
            merged_note = ""
            if getattr(args, "merge_draft", False):
                # Rework MEDIA/SEO/TRAP de um post bloqueado: o patch parcial e
                # mesclado DETERMINISTICAMENTE com o draft original — o agente
                # nunca precisa reenviar um artigo inteiro para corrigir um
                # componente (o merge e do codigo, nao do modelo).
                payload, merged_note = _merge_patch_with_draft(
                    args.root, args.post_id, payload
                )
            # Reserva ATOMICA da vaga: checar e registrar nao pode ter janela
            # entre dois processos (cron + manual) — senao os dois passariam pelo
            # teto. Em dry-run nada e reservado; com o budget de CONTEXTO
            # estourado um post NOVO nem chega a reservar (a sessao acabou).
            projecao_antes = session_budget.status(args.root, config)
            ja_tocado = int(args.post_id) in projecao_antes["posts_touched"]
            motivo = session_budget.stop_reason(args.root, config)
            dry = bool(getattr(args, "dry_run", False))
            if dry:
                permitido, orcamento = session_budget.touch_allowed(
                    args.root, args.post_id, config
                )
                sem_vaga = not permitido
            elif motivo and not ja_tocado:
                permitido, orcamento, sem_vaga = False, projecao_antes, True
            else:
                permitido, orcamento = session_budget.claim_touch(
                    args.root, args.post_id, config
                )
                sem_vaga = not permitido
            if not dry and sem_vaga:
                # Encerra a sessao ANTES de escrever: o post fica intacto para a
                # proxima janela (nenhum estado/attempt e consumido, nenhum gate
                # e afrouxado — o unico efeito e a sessao parar de crescer).
                result = {
                    "post_id": args.post_id,
                    "status": "session_budget_exhausted",
                    "wordpress_changed": False,
                    "reason": motivo
                    or (
                        "teto de posts tocados por sessao atingido "
                        f"({orcamento['posts_touched_count']}/{orcamento['max_posts_touched']})"
                    ),
                    "action": "nenhum comando novo: encerre a sessao; o post fica para a proxima janela",
                    "session": orcamento,
                }
            else:
                result = apply_editorial(client, config, args.root, args.post_id, payload)
                if merged_note:
                    result["merged_from_draft"] = merged_note
                if not result.get("dry_run") and result.get("status") == "ready":
                    session_budget.record_ready(args.root, config)
                if args.compact:
                    # Auditoria completa em arquivo; terminal so com o resumo
                    # (success = minimo, failure = so o que corrigir).
                    audit = args.root / "backups" / str(args.post_id) / "apply.latest.json"
                    _write_audit(audit, result)
                    result = _compact_apply(result)
                result.setdefault("session", session_budget.status(args.root, config))
        _record_cmd_output(args, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ConfigError, EditorialProviderError, WordPressError, WorkflowError, OSError, ValueError) as exc:
        # WorkflowError incluido de proposito: um traceback Python no contexto do
        # agente e caro e inutil — o operador (e o LLM) precisam do motivo em
        # JSON, nao da stack inteira.
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


# --- Regra arquitetural (P1 da auditoria de contexto) ----------------------
# Todo comando PESADO gera DOIS produtos: um arquivo detalhado para
# debug/auditoria e um stdout pequeno orientado a proxima acao. O agente nunca
# carrega o que so serve para auditoria; o operador nunca perde o detalhe.
def _write_audit(path: Path, payload: Any) -> str:
    """Grava o produto de auditoria de um comando pesado (fail-soft)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(path)
    except Exception:  # noqa: BLE001 - auditoria nunca derruba o comando
        return ""


# Campos do candidato que o AGENTE precisa para decidir (o resto — evidence
# completo, pHash, flags internas, rejeitados — fica no arquivo de auditoria).
def _media_candidate(cand: dict) -> dict:
    """Projecao minima de um candidato utilizavel (economia de contexto)."""
    return {
        "url": cand.get("direct_image_url"),
        "source": cand.get("source_page_url"),
        "score": cand.get("evidence_score"),
        "verdict": (cand.get("evidence") or {}).get("verdict"),
        "official_source": bool(cand.get("official_source")),
        "already_in_library": bool(cand.get("already_in_library")),
        "needs_vision": bool(cand.get("needs_vision")),
    }


def _rejected_summary(rejeitados: list[dict]) -> dict[str, int]:
    """Rejeitados viram um RESUMO por motivo (nunca a lista completa)."""
    resumo: dict[str, int] = {}
    for cand in rejeitados:
        verdict = str((cand.get("evidence") or {}).get("verdict") or "")
        motivo = verdict or str(cand.get("rejected_reason") or "unknown")
        resumo[motivo] = resumo.get(motivo, 0) + 1
    return resumo


def _media_decision(aprovados: list[dict], *, options: int = 3, margin: int = 2) -> dict:
    """Selecao DETERMINISTICA: o modelo so decide quando ha ambiguidade real.

    O codigo ja ordenou por ``already_in_library`` -> ``official_source`` ->
    ``evidence_score``. Se existe UM candidato inequivoco que passou todos os
    hard gates (``deterministic_match`` sem empate no topo), ele vai sozinho
    para o ``media-validate``. Em caso de empate/ambiguidade, vao 2-3 opcoes
    para julgamento (a visao continua reservada a esses casos).

    ``margin`` e a margem minima de ``evidence_score`` para considerar o topo
    ISOLADO (``EDITOR_AUTO_SCORE_MARGIN``, default 2). E HIPOTESE DE CALIBRACAO,
    nao fato: a decisao carrega ``score_gap`` e a RAZAO, e a telemetria cruza
    decisao x resultado dos gates (media_validate/apply/first_pass) — se os
    casos ``auto`` comecarem a ser rejeitados depois, o parametro sobe.
    """
    fortes = [c for c in aprovados if (c.get("evidence") or {}).get("verdict") == "deterministic_match"]
    fracos = [c for c in aprovados if (c.get("evidence") or {}).get("verdict") != "deterministic_match"]
    if fortes:
        nota_topo = int(fortes[0].get("evidence_score") or 0)
        nota_segundo = int(fortes[1].get("evidence_score") or 0) if len(fortes) > 1 else None
        gap = None if nota_segundo is None else nota_topo - nota_segundo
        unico = len(fortes) == 1 or (gap or 0) >= int(margin)
    else:
        nota_topo = nota_segundo = gap = None
        unico = False
    if unico:
        escolhido = _media_candidate(fortes[0])
        if len(fortes) == 1:
            razao = f"unico candidato forte (score {nota_topo}); hard gates passaram"
        else:
            razao = (
                f"melhor candidato forte isolado (score {nota_topo} vs {nota_segundo}; "
                f"gap {gap} >= margem {margin})"
            )
        return {
            "decision": "auto",
            "reason": razao,
            "score_gap": gap,
            "select": escolhido,
            "options": [escolhido],
        }
    ordenados = (fortes + fracos)[: max(1, options)]
    if not ordenados:
        return {
            "decision": "none",
            "reason": "nenhum candidato aprovado (todos reprovaram em gate de origem/relevancia)",
            "score_gap": None,
            "select": None,
            "options": [],
        }
    if fortes:
        razao = (
            f"empate de candidatos fortes (scores {nota_topo} vs {nota_segundo}; "
            f"gap {gap} < margem {margin}): {len(ordenados)} opcoes para julgamento"
        )
    else:
        razao = (
            f"sem candidato forte; {len(ordenados)} opcao(oes) ambigua(s) "
            "(visao decide)"
        )
    return {
        "decision": "choose",
        "reason": razao,
        "score_gap": gap,
        "select": None,
        "options": [_media_candidate(c) for c in ordenados],
    }


def _final_decision(
    aprovados: list[dict], reuso: list[dict], needed: int, *, margin: int = 2
) -> dict:
    """Decisão FINAL de mídia (auto/choose/reuse/none) + COBERTURA do déficit.

    Duas perguntas diferentes, dois campos diferentes:

    * ``coverage`` — DE ONDE vieram as imagens: ``local`` (o acervo cobre tudo),
      ``mixed`` (parte do acervo + parte da web) ou ``web`` (só web);
    * ``decision`` — se o material da WEB exige julgamento (``auto``/``choose``)
      ou se nada é necessário (``reuse``/``none``).

    ``reuse`` só vale quando o acervo local cobre a necessidade INTEIRA
    (``len(reuso) >= needed``). Antes a conta era
    ``faltam = needed - len(reuso) - fortes`` e o caso misto (1 local + 1 web com
    needed=2) virava ``decision=reuse`` — dizendo ao agente que não havia busca
    a fazer quando metade do material vinha da web, e contradizendo o próprio
    SKILL (`reuse` = acervo cobre o déficit e não houve busca web).
    """
    fortes = sum(
        1 for c in aprovados if (c.get("evidence") or {}).get("verdict") == "deterministic_match"
    )
    cobertura = "local" if len(reuso) >= int(needed) else ("mixed" if reuso else "web")
    decisao = _media_decision(aprovados, margin=int(margin))
    decisao["coverage"] = cobertura
    decisao["local_reuse"] = len(reuso)
    if cobertura == "local":
        decisao = {
            "decision": "reuse",
            "reason": (
                f"acervo local cobre o deficit ({len(reuso)} reuso(s) para "
                f"{int(needed)} necessarias); nenhuma busca web necessaria"
            ),
            "coverage": "local",
            "local_reuse": len(reuso),
            "score_gap": None,
            "select": None,
            "options": decisao.get("options") or [],
        }
    elif cobertura == "mixed" and decisao.get("decision") in ("auto", "choose"):
        # Material local PARCIAL: a decisão continua sendo sobre o candidato da
        # web (é ele que vai para o media-validate), mas a cobertura diz a
        # verdade sobre a origem das imagens.
        decisao["reason"] = (
            f"cobertura mista ({len(reuso)} do acervo local + candidato da web); "
            + str(decisao.get("reason") or "")
        )
    return decisao


def _compact_media_search(
    *,
    query: str,
    subject: str,
    needed: int,
    reuso: list[dict],
    aprovados: list[dict],
    rejeitados: list[dict],
    engines: list[str],
    audit: str,
    ambiguous: int = 0,
    deferred: int = 0,
    margin: int = 2,
    decision_id: str = "",
) -> dict:
    """Contrato compacto do ``media-search-web`` (o que o agente pode USAR).

    Rejeitados nao vem inteiros: viram ``rejected_summary`` (contagem por
    motivo). O JSON completo (candidatos brutos, evidencia, pHash, flags,
    pagina de origem de cada rejeitado) fica em ``work/search/<chave>.json``.

    ``deferred`` sai em campo PRÓPRIO: candidato que deixou de ser investigado
    porque a capacidade já estava atendida não é rejeição.
    """
    fortes = sum(
        1 for c in aprovados if (c.get("evidence") or {}).get("verdict") == "deterministic_match"
    )
    decisao = _final_decision(aprovados, reuso, needed, margin=int(margin))
    faltam = max(0, int(needed) - len(reuso) - fortes)
    resultado = {
        "query": query,
        "subject": subject,
        "needed": int(needed),
        "capacity": {
            "needed": int(needed),
            "reuse": len(reuso),
            "strong": fortes,
            "ambiguous": int(ambiguous),
            "accepted": len(aprovados),
            "deferred": int(deferred),
            "missing": faltam,
        },
        "engines_queried": engines,
        "audit": audit,
        "decision_reason": decisao.get("reason"),
        "decision_score_gap": decisao.get("score_gap"),
        # Cobertura diz DE ONDE vieram as imagens (local/mixed/web); decision diz
        # se o material da web exige julgamento. Também vai o decision_id do
        # ledger: é ele que o media-validate/apply referenciam.
        "coverage": decisao.get("coverage") or ("local" if reuso and not aprovados else "web"),
        "decision_id": decision_id,
    }
    if reuso:
        resultado["reuse"] = reuso
    resultado.update(decisao)
    resultado["rejected_summary"] = _rejected_summary(rejeitados)
    resultado["rejected_total"] = len(rejeitados)
    if faltam and not reuso and not aprovados:
        resultado["action"] = (
            "sem candidato utilizavel: NAO force apply — registre uncertain (ou "
            "tente outro termo do MESMO subject)"
        )
    elif faltam:
        resultado["action"] = (
            f"faltam {faltam} imagem(ns) forte(s): use 'reuse' primeiro e depois "
            "rode media-search-web de novo com outro termo do mesmo subject"
        )
    return resultado


def _compact_media_validate(
    editorial: dict,
    result: dict,
    *,
    audit: str,
    post_title: str = "",
) -> dict:
    """Contrato compacto do ``media-validate`` (o agente só precisa do delta).

    ``valid`` / ``rejected[{index,reason}]`` / ``capacity{required,valid,missing}``
    / ``featured{status,reason}``. O agente não carrega dados de SUCESSO (visão
    detalhada, evidência, lista completa): ele precisa saber o que falhou e
    quanto falta. O relatório completo fica em ``work/media-validate/*.json``
    (e em ``--full``).
    """
    from .content_quality import word_count

    plano = editorial.get("media_plan") or []
    html = str(editorial.get("cleaned_html") or "")
    titulo = post_title or str((editorial.get("seo") or {}).get("title") or "")
    required = required_image_count(word_count(html), title=titulo, content=html)
    rejeitados_idx = {
        int(row.get("index"))
        for row in (result.get("rejected") or [])
        if isinstance(row.get("index"), int)
    }
    inline_ok = sum(
        1
        for index, item in enumerate(plano)
        if index not in rejeitados_idx and not item.get("is_featured")
    )
    capacity: dict[str, Any] = {
        "required": required,
        "valid": inline_ok,
        "missing": max(0, required - inline_ok),
    }
    visao = result.get("featured_vision") or []
    if visao:
        primeira = visao[0] if isinstance(visao[0], dict) else {}
        featured = {
            "status": primeira.get("status"),
            "reason": str(primeira.get("reason") or "")[:200],
        }
    elif any(item.get("is_featured") for item in plano):
        featured = {"status": "passed", "reason": "featured do plano aceita no preflight"}
    else:
        featured = {
            "status": "absent",
            "reason": "plano sem featured: o codigo normaliza a featured existente",
        }
    listicle = result.get("listicle") or {}
    if listicle.get("applicable"):
        capacity["verified_inline_capacity"] = listicle.get("verified_inline_capacity")
        capacity["promised_items"] = listicle.get("promised_items")
    return {
        "valid": result.get("valid"),
        "rejected": [
            {"index": row.get("index"), "reason": str(row.get("reason") or "")[:200]}
            for row in (result.get("rejected") or [])
        ],
        "capacity": capacity,
        "featured": featured,
        "audit": audit,
    }


def _audit_key(query: str, post_id: int = 0) -> str:
    """Nome de arquivo estavel (e seguro) para o produto de auditoria."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(query or "")).strip("-").lower()[:60]
    return f"{slug or 'busca'}-{int(post_id or 0)}"


def _reuse_from_library(
    client, root, subject: str, *, limit: int
) -> list[dict]:
    """Media Library + indice local ANTES da busca web (P1 da auditoria).

    Inverte o fluxo: ``subject -> indice/Media Library -> proveniencia/pHash ->
    deficit restante -> web``. A imagem reutilizada JA passou pelos controles
    (proveniencia registrada: URL original + pagina de origem + pHash +
    media_id), entao reusar nao afrouxa nenhum gate — apenas evita gastar a
    busca para descobrir que a imagem ja existia. So valida que o attachment
    ainda existe no WordPress.

    Dois caminhos deterministicos, unidos por URL de origem:

    * indice por SUBJECT (entradas novas: o media_plan carrega o subject);
    * Media Library por SUBJECT (title/alt/caption) -> indice por media_id
      (entradas antigas ficaram sem subject, mas tem proveniencia).
    """
    if not str(subject or "").strip():
        return []
    reuso: list[dict] = []
    vistos: set[str] = set()

    def _adicionar(entrada: dict) -> None:
        if len(reuso) >= max(1, limit):
            return
        url = str(entrada.get("source_url") or "")
        pagina = str(entrada.get("source_page") or "")
        # Sem origem registrada o reuso NAO e utilizavel: o apply exige a URL
        # listada na pagina original.
        if not (url and pagina) or url in vistos:
            return
        vistos.add(url)
        reuso.append(
            {
                "url": url,
                "source": pagina,
                "media_id": entrada.get("media_id"),
                "phash": str(entrada.get("phash") or ""),
                "subject": entrada.get("subject"),
            }
        )

    try:
        from .media.library_index import find_by_media_id, find_by_subject

        for entrada in find_by_subject(root, subject):
            media_id = entrada.get("media_id")
            if media_id:
                try:
                    client.get_media(int(media_id))
                except Exception:  # noqa: BLE001 - attachment removido
                    continue
            _adicionar(entrada)
    except Exception:  # noqa: BLE001 - indice e otimizacao
        pass

    if len(reuso) < max(1, limit):
        try:
            # A Media Library guarda o nome da obra no title/alt/caption do
            # attachment: e o caminho que acha o acervo ANTIGO (sem subject no
            # indice) e evita a busca web inteira.
            from .media.library_index import find_by_media_id

            for item in client.search_media(subject, per_page=max(3, limit * 3)):
                if len(reuso) >= max(1, limit):
                    break
                media_id = item.get("id")
                if not isinstance(media_id, int):
                    continue
                entrada = find_by_media_id(root, media_id)
                if entrada:
                    _adicionar(entrada)
        except Exception:  # noqa: BLE001 - Media Library e otimizacao
            pass
    return reuso


# Gate do checklist -> componente do rework (o agente corrige SO o componente).
_GATE_COMPONENT = {
    "imagens_no_corpo": "media",
    "relevancia_imagens": "media",
    "imagens_duplicadas": "media",
    "imagens_similares": "media",
    "imagens_webp": "media",
    "imagens_visao": "media",
    "dimensoes_imagens": "media",
    "destaque_relevancia": "media",
    "qualidade_texto": "text",
    "estrutura_lista": "text",
    "conteudo_nao_vazio": "text",
    "cta_canonico": "text",
    "fonte_original_link": "text",
    "schema_editorial": "seo",
    "trailer_youtube": "trailer",
}

_COMPONENT_KEYS = {
    "media": ("media_plan", "game_name"),
    "seo": ("seo", "game_name"),
    "text": ("cleaned_html", "seo"),
    "trailer": ("needs_trailer", "trailer_url", "game_name"),
}


def _blocked_components(root: Path, post_id: int) -> list[str]:
    """Componentes do rework inferidos do ``editorial.blocked.json``."""
    try:
        data = json.loads(
            (root / "backups" / str(post_id) / "editorial.blocked.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return []
    checklist = data.get("blocked_checklist")
    if not isinstance(checklist, dict):
        return []
    componentes: list[str] = []
    for item in checklist.get("items") or []:
        if item.get("status") not in ("fail", "error"):
            continue
        componente = _GATE_COMPONENT.get(str(item.get("name") or ""))
        if componente and componente not in componentes:
            componentes.append(componente)
    return componentes


def _draft_for_fix(root: Path, post_id: int, draft: dict, components: list[str]) -> dict:
    """Extrato do draft com APENAS o(s) componente(s) do rework + o erro.

    O agente que vai corrigir uma imagem nao precisa receber SEO, texto e o
    HTML inteiro de volta: ele recebe o ``media_plan``, a featured do plano, o
    subject e o erro do gate. O resto continua no arquivo (``full_draft``).
    """
    if not components:
        components = ["media"]
    extrato: dict[str, Any] = {
        "post_id": post_id,
        "component": components[0] if len(components) == 1 else components,
        "full_draft": str(root / "backups" / str(post_id) / "editorial.draft.json"),
    }
    for componente in components:
        for chave in _COMPONENT_KEYS.get(componente, ()):
            if chave in draft and chave not in extrato:
                extrato[chave] = draft[chave]
    if "media" in components:
        plano = draft.get("media_plan") or []
        extrato["featured"] = next(
            (item for item in plano if isinstance(item, dict) and item.get("is_featured")),
            None,
        )
    extrato["subject"] = str((draft.get("seo") or {}).get("title") or draft.get("game_name") or "")
    extrato["error"] = _blocked_error(root, post_id)
    # P2: rework de midia/SEO/trailer NAO precisa do artigo na conversa.
    extrato["requires_content"] = "text" in components
    extrato["apply_hint"] = (
        f"unicornio-editor apply {post_id} <patch.json> --merge-draft --compact "
        "(o patch traz SO o componente corrigido; o codigo mescla com o draft)"
    )
    return extrato


def _blocked_error(root: Path, post_id: int) -> dict:
    """Erro do gate que bloqueou o post (motivo + itens que falharam)."""
    try:
        data = json.loads(
            (root / "backups" / str(post_id) / "editorial.blocked.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return {}
    checklist = data.get("blocked_checklist") or {}
    return {
        "reason": data.get("blocked_reason") or data.get("reason") or "",
        "failed": [
            {"name": item.get("name"), "detail": str(item.get("detail") or "")[:200]}
            for item in (checklist.get("items") or [])
            if item.get("status") in ("fail", "error")
        ],
    }


def _merge_patch(draft: dict, patch: dict) -> dict:
    """Merge DETERMINISTICO do patch parcial sobre o draft original.

    - chaves de METADADO do agente (prefixo ``_``, ``patch``) nao vao para o
      editorial;
    - dicionarios (``site_relevance``, ``seo``, trailer) mesclam CHAVE A CHAVE:
      o patch corrige so o que mudou;
    - listas (``media_plan``) e escalares SUBSTITUEM o valor do draft (o
      componente corrigido vai por inteiro).
    """
    mesclado = dict(draft)
    for chave, valor in (patch or {}).items():
        if chave.startswith("_") or chave in {"patch", "merge"}:
            continue
        atual = mesclado.get(chave)
        if isinstance(valor, dict) and isinstance(atual, dict):
            combinado = dict(atual)
            combinado.update(valor)
            mesclado[chave] = combinado
        else:
            mesclado[chave] = valor
    return mesclado


def _merge_patch_with_draft(root: Path, post_id: int, patch: dict) -> tuple[dict, str]:
    """Aplica o patch do agente sobre o draft e grava o resultado (auditoria)."""
    if not isinstance(patch, dict):
        raise ValueError("--merge-draft exige um objeto JSON (patch parcial)")
    uteis = [
        chave for chave in patch
        if not chave.startswith("_") and chave not in {"patch", "merge"}
    ]
    if not uteis:
        raise ValueError(
            "--merge-draft exige ao menos um componente no patch "
            "(ex.: {\"media_plan\": [...]} ou {\"seo\": {...}})"
        )
    draft = load_draft(root, post_id)
    mesclado = _merge_patch(draft, patch)
    destino = root / "backups" / str(post_id) / "editorial.merged.json"
    _write_audit(destino, mesclado)
    return mesclado, f"draft + patch ({', '.join(sorted(uteis))}) -> {destino}"


# Comandos de LEITURA que alimentam o contexto do LLM (o custo de tokens que
# queremos medir). Inclui `media-validate` (comando de leitura pesado: media
# plan + visao da featured) — sem ele o `context_bytes_total` ficava subestimado.
_CONTEXT_CMDS = frozenset(
    {"list-pending", "queue", "cards", "prepare", "draft", "content",
     "prepare-batch", "editorial-generate-batch", "media-search", "media-search-web", "media-search-listicle", "vision-batch", "media-resolve-batch", "checklist",
     "telemetry", "media-validate"}
)

# Comandos de ESCRITA que tambem colocam contexto no LLM (o resultado do apply
# e o que decide o proximo passo). Medidos no MESMO evento, distinguidos por
# `kind`, para o operador conseguir somar o custo real por post.
_WRITE_CONTEXT_CMDS = frozenset(
    {"apply", "apply-batch", "uncertain", "discard", "retry", "publish"}
)

# O ledger da SESSAO editorial so aceita comandos do proprio agente editorial.
# `publish`/`publish-ready` rodam em OUTRO cron (janelas de publicacao) no mesmo
# projeto: contabilizar o contexto deles estenderia a janela do ledger e a
# execucao seguinte de editorial herdaria o teto esgotado da anterior.
_SESSION_LEDGER_CMDS = (
    _CONTEXT_CMDS | frozenset({"apply", "apply-batch", "editorial-generate-batch", "uncertain", "discard", "retry"})
)


def _result_sizes(args: argparse.Namespace, result: Any) -> dict[str, Any]:
    """Tamanhos/campos que explicam QUANTO cada etapa colocou no contexto."""
    detalhe: dict[str, Any] = {}
    post_id = getattr(args, "post_id", None)
    if isinstance(post_id, int) and post_id:
        detalhe["post_id"] = post_id
    if not isinstance(result, dict):
        if isinstance(result, list):
            detalhe["items"] = len(result)
        return detalhe
    batch_id = result.get("batch_id")
    if isinstance(batch_id, str) and batch_id:
        detalhe["batch_id"] = batch_id
        detalhe["batch_size"] = int(result.get("count") or len(result.get("items") or []))
        detalhe["batch_prepared"] = int(result.get("prepared") or 0)
        detalhe["batch_failed"] = int(result.get("failed") or 0)
    payload = result.get("payload")
    if isinstance(payload, dict):
        detalhe["candidates"] = len(payload.get("candidates") or [])
        detalhe["rejected"] = len(payload.get("rejected") or [])
    if isinstance(result.get("candidates"), list):
        detalhe["candidates"] = len(result["candidates"])
    if isinstance(result.get("rejected"), list):
        detalhe["rejected"] = len(result["rejected"])
    for chave, nome in (
        ("valid", "valid"), ("listicle", "listicle"),
    ):
        if chave in result and isinstance(result[chave], (int, bool)):
            detalhe[nome] = result[chave]
    for chave in ("cleaned_html", "draft"):
        valor = result.get(chave)
        if isinstance(valor, str) and valor:
            detalhe[f"{chave}_bytes"] = len(valor.encode("utf-8"))
    editorial_file = getattr(args, "editorial_file", None)
    if editorial_file is not None:
        try:
            detalhe["editorial_bytes"] = Path(editorial_file).stat().st_size
        except OSError:
            pass
    return detalhe


def _record_cmd_output(args: argparse.Namespace, result: Any) -> None:
    """Registra quantos bytes de contexto um comando produziu.

    Cada chamada imprime um JSON que o LLM consome como contexto; esse tamanho e
    o custo de tokens real da run. Grava ``cmd_output`` no telemetry.jsonl
    central (fail-soft) com o ``post_id`` e o tamanho dos componentes
    (``cleaned_html``/draft) quando existem — e alimenta o orcamento de contexto
    da sessao, para a sessao encerrar limpa quando o budget estourar.
    """
    command = getattr(args, "command", None)
    if command not in _CONTEXT_CMDS and command not in _WRITE_CONTEXT_CMDS:
        return
    try:
        payload = json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return
    tamanho = len(payload.encode("utf-8"))
    root = getattr(args, "root", Path("."))
    try:
        from .observability import append_telemetry

        append_telemetry(
            root,
            "cmd_output",
            command=command,
            kind="read" if command in _CONTEXT_CMDS else "write",
            bytes=tamanho,
            **_result_sizes(args, result),
        )
    except Exception:  # noqa: BLE001 - telemetria nunca derruba o CLI
        pass
    try:
        from .config import load_config
        from . import session_budget

        if command in _SESSION_LEDGER_CMDS:
            session_budget.record_context_bytes(root, tamanho, load_config())
    except Exception:  # noqa: BLE001 - orcamento nunca derruba o CLI
        pass


if __name__ == "__main__":
    raise SystemExit(main())
