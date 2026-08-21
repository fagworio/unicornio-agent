"""Command-line entry point for the editorial agent."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from .backup import SnapshotStore
from .checklist import run_pre_publish_checklist
from .config import ConfigError, load_config
from .editorial_schema import validate_editorial
from .html_cleaner import clean_html
from .maintenance import generate_report
from .workflow import (
    apply_editorial,
    compose_final_content,
    original_link_of,
    prepare_post,
    publish_post,
    publish_ready_posts,
)
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

    checklist_parser = subparsers.add_parser(
        "checklist", help="roda o checklist pre-publicacao (somente leitura)"
    )
    checklist_parser.add_argument("post_id", type=int)
    checklist_parser.add_argument("editorial_file", type=Path)
    checklist_parser.add_argument("--root", type=Path, default=Path("."))

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
        client = WordPressClient(config)
        if args.command == "list-pending":
            result = client.list_pending(page=args.page, per_page=config.batch_limit)
            if args.compact:
                result = _compact_listing(result)
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
            if editorial.get("cleaned_html") is None and editorial["site_relevance"]["decision"] == "process":
                raw = (post.get("content") or {}).get("raw")
                if not isinstance(raw, str):
                    raise WordPressError("post content.raw is missing")
                editorial = {**editorial, "cleaned_html": clean_html(raw)}
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
        elif args.command == "publish":
            result = publish_post(client, config, args.root, args.post_id)
        elif args.command == "publish-ready":
            outcomes = publish_ready_posts(client, config, args.root, limit=config.publish_limit)
            published = [o for o in outcomes if o.get("wordpress_changed")]
            blocked = [o for o in outcomes if o.get("status") in ("blocked", "error")]
            if published or blocked:
                result = {
                    "published": len(published),
                    "posts": published,
                    "blocked_or_skipped": len(outcomes) - len(published),
                    "quality_blocked": len(blocked),
                    "blocked_posts": [
                        {
                            "post_id": outcome.get("post_id"),
                            "status": outcome.get("status"),
                            "reason": outcome.get("reason"),
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
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ConfigError, WordPressError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
