"""Command-line entry point for the editorial agent."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .backup import SnapshotStore
from .checklist import run_pre_publish_checklist
from .config import ConfigError, load_config
from .editorial_schema import validate_editorial
from .maintenance import generate_report
from .workflow import (
    apply_editorial,
    compose_final_content,
    original_link_of,
    prepare_post,
    publish_post,
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

    prepare_parser = subparsers.add_parser("prepare", help="cria snapshot e relatório")
    prepare_parser.add_argument("post_id", type=int)
    prepare_parser.add_argument("--root", type=Path, default=Path("."))

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
    subparsers.add_parser(
        "publish-ready",
        help="publica todos os pending prontos (checklist ok); silencioso quando nao ha nada",
    )

    report_parser = subparsers.add_parser("maintenance-report", help="gera diagnóstico sem escrever")
    report_parser.add_argument("report_file", type=Path)
    report_parser.add_argument("--broken-url", action="append", default=[])
    report_parser.add_argument("--min-inline-images", type=int, default=1)
    return parser


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
        elif args.command == "prepare":
            result = prepare_post(client, args.root, args.post_id)
        elif args.command == "checklist":
            payload = json.loads(args.editorial_file.read_text(encoding="utf-8"))
            editorial = validate_editorial(payload, min_confidence=config.min_relevance_confidence)
            post = client.get_post(args.post_id)
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
            posts = client.list_pending(per_page=50)
            outcomes = []
            for candidate in posts:
                candidate_id = candidate.get("id")
                if not isinstance(candidate_id, int):
                    continue
                try:
                    outcome = publish_post(client, config, args.root, candidate_id)
                except Exception as exc:  # noqa: BLE001 - report per post, keep the loop alive
                    outcome = {
                        "post_id": candidate_id,
                        "wordpress_changed": False,
                        "status": "error",
                        "reason": str(exc),
                    }
                outcomes.append(outcome)
            if any(outcome.get("wordpress_changed") for outcome in outcomes):
                published = [o for o in outcomes if o.get("wordpress_changed")]
                result = {
                    "published": len(published),
                    "posts": published,
                    "blocked_or_skipped": len(outcomes) - len(published),
                }
            else:
                # Watchdog pattern: stay completely silent when nothing happened.
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
