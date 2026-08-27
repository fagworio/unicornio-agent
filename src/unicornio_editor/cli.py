"""Command-line entry point for the editorial agent."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from .backup import SnapshotStore
from .checklist import run_pre_publish_checklist
from .config import ConfigError, load_config
from .editorial_schema import validate_editorial
from .maintenance import generate_report
from .workflow import (
    apply_editorial,
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
from .state import STATE_AWAITING_HUMAN, STATE_BLOCKED
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

    telemetry_parser = subparsers.add_parser(
        "telemetry",
        help="resumo agregado das blocagens/resultados do pipeline (work/telemetry.jsonl; "
        "somente leitura) — responde 'a fila parou por que?'",
    )
    telemetry_parser.add_argument("--root", type=Path, default=Path("."))

    cards_parser = subparsers.add_parser(
        "cards",
        help="cartoes compactos dos posts pending (entidades, gaps, SEO, imagens, dica de jogo) — "
        "economia de tokens: UMA chamada substitui list-pending+prepare+leituras",
    )
    cards_parser.add_argument("--root", type=Path, default=Path("."))
    cards_parser.add_argument("--limit", type=int, default=None, help="maximo de cartoes (default: EDITOR_BATCH_LIMIT)")

    prepare_parser = subparsers.add_parser("prepare", help="cria snapshot e relatório")
    prepare_parser.add_argument("post_id", type=int)
    prepare_parser.add_argument("--root", type=Path, default=Path("."))
    prepare_parser.add_argument(
        "--compact",
        action="store_true",
        help="grava o JSON completo em backups/<id>/prepared.json e imprime apenas "
        "o resumo (economia de tokens); leia o arquivo para obter o cleaned_html",
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

    media_search_web_parser = subparsers.add_parser(
        "media-search-web",
        help="descobre candidatos de imagem via buscadores (Bing primario, Google/Yandex "
        "fallback) com filtro de tamanho (somente leitura; index de descoberta, a fonte "
        "e a pagina original)",
    )
    media_search_web_parser.add_argument("termo", type=str, help="termo de busca (ex.: redfall xbox series)")
    media_search_web_parser.add_argument(
        "--size", default="xga", help="classe de tamanho (default: xga = 1024x768)"
    )
    media_search_web_parser.add_argument(
        "--ratio", default="w", help="proporcao (default: w = larga)"
    )
    media_search_web_parser.add_argument(
        "--limit", type=int, default=10, help="maximo de candidatos (default: 10)"
    )
    media_search_web_parser.add_argument(
        "--engine", default="auto",
        help="buscador: auto (rotaciona Bing->Google->Yandex), bing, google, yandex "
        "(default: auto)",
    )
    media_search_web_parser.add_argument("--root", type=Path, default=Path("."))

    content_parser = subparsers.add_parser(
        "content",
        help="retorna o cleaned_html do post (somente leitura; use SO quando for "
        "reescrever o texto, em vez de abrir o prepared.json inteiro)",
    )
    content_parser.add_argument("post_id", type=int)
    content_parser.add_argument("--root", type=Path, default=Path("."))

    media_validate_parser = subparsers.add_parser(
        "media-validate",
        help="valida o media_plan de um editorial.json SEM executar download/upload "
        "(1 chamada compacta; corriga o plano antes do apply)",
    )
    media_validate_parser.add_argument("editorial_file", type=Path)
    media_validate_parser.add_argument("--root", type=Path, default=Path("."))

    draft_parser = subparsers.add_parser(
        "draft",
        help="imprime o editorial.draft.json do post (base do rework incremental; "
        "leia SO para corrigir o componente apontado pelo fix do card)",
    )
    draft_parser.add_argument("post_id", type=int)
    draft_parser.add_argument("--root", type=Path, default=Path("."))

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
        title = (post.get("title") or {}).get("raw") or (post.get("title") or {}).get("rendered")
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
            "wordpress_changed": False,
            "failed": failed or reasons,
        }
        if images:
            compact["images"] = images
        return compact
    if result.get("status") == "uncertain":
        return {
            "post_id": post_id,
            "status": "uncertain",
            "wordpress_changed": False,
            "skip_reason": result.get("skip_reason"),
        }
    if result.get("status") == "skipped":
        return {
            "post_id": post_id,
            "status": "skipped",
            "wordpress_changed": False,
            "skip_reason": result.get("skip_reason"),
        }
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
            from .observability import read_telemetry_summary

            result = read_telemetry_summary(args.root)
        elif args.command == "queue":
            report = build_queue_report(client, args.root)
            if args.monitor:
                # Linha estavel hasheada pelo cron do Hermes (--monitor-script):
                # so muda quando ha trabalho elegivel real, nunca a cada tick.
                print(_monitor_line(report))
                return 0
            result = report
        elif args.command == "cards":
            result = build_cards(client, config, args.root, per_page=args.limit)
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
        elif args.command == "checklist":
            payload = json.loads(args.editorial_file.read_text(encoding="utf-8"))
            editorial = validate_editorial(payload, min_confidence=config.min_relevance_confidence)
            post = client.get_post(args.post_id)
            if editorial["site_relevance"]["decision"] == "process":
                editorial = resolve_editorial_defaults(editorial, post)
            backup = SnapshotStore(args.root).save(args.post_id, post)
            content, trailer = compose_final_content(editorial, config, original_link_of(post))
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
        elif args.command == "media-search-web":
            from .media.search import search_web_images
            from .observability import append_telemetry

            candidates = search_web_images(
                args.termo,
                size=args.size,
                ratio=args.ratio,
                limit=args.limit,
                engine=getattr(args, "engine", "auto"),
            )
            if not candidates:
                # Possivel bloqueio/rate-limit do Google em producao: registrar
                # para o operador distinguir "nao ha imagem" de "busca falhou".
                append_telemetry(
                    args.root, "media_search_empty",
                    query=args.termo,
                    size_filter=f"{args.size}|{args.ratio}",
                )
            result = {
                "query": args.termo,
                "size_filter": f"{args.size}|{args.ratio}",
                "count": len(candidates),
                "candidates": candidates,
            }
        elif args.command == "content":
            result = get_cleaned_content(client, args.root, args.post_id)
        elif args.command == "media-validate":
            payload = json.loads(args.editorial_file.read_text(encoding="utf-8"))
            result = validate_media_plan(client, payload)
        elif args.command == "draft":
            result = load_draft(args.root, args.post_id)
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
            payload = json.loads(args.editorial_file.read_text(encoding="utf-8"))
            result = apply_editorial(client, config, args.root, args.post_id, payload)
            if args.compact:
                # Auditoria completa em arquivo; terminal so com o resumo
                # (success = minimo, failure = so o que corrigir).
                audit = args.root / "backups" / str(args.post_id) / "apply.latest.json"
                audit.parent.mkdir(parents=True, exist_ok=True)
                audit.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                result = _compact_apply(result)
        _record_cmd_output(args, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ConfigError, WordPressError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


# Comandos de LEITURA que alimentam o contexto do LLM (o custo de tokens que
# queremos medir). Comandos de escrita (apply/publish/retry/...) tem eventos
# proprios na telemetria e nao entram aqui.
_CONTEXT_CMDS = frozenset(
    {"list-pending", "queue", "cards", "prepare", "draft", "content",
     "media-search", "media-search-web", "checklist", "telemetry"}
)


def _record_cmd_output(args: argparse.Namespace, result: Any) -> None:
    """Registra quantos bytes de contexto um comando de leitura produziu.

    Cada chamada de leitura imprime um JSON que o LLM consome como contexto;
    esse tamanho e o custo de tokens real da run. Grava ``cmd_output`` no
    telemetry.jsonl central (fail-soft) para o operador somar o gasto por run.
    """
    command = getattr(args, "command", None)
    if command not in _CONTEXT_CMDS:
        return
    try:
        payload = json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return
    try:
        from .observability import append_telemetry

        append_telemetry(
            getattr(args, "root", Path(".")),
            "cmd_output",
            command=command,
            bytes=len(payload.encode("utf-8")),
        )
    except Exception:  # noqa: BLE001 - telemetria nunca derruba o CLI
        pass


if __name__ == "__main__":
    raise SystemExit(main())
