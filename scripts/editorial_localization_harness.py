#!/usr/bin/env python3
"""Run the editorial provider in an isolated, auditable local harness.

This script deliberately does not import the WordPress client or V2 lifecycle.
It only calls the existing editorial provider, validates its response, and
writes the input/output/checklist/diagnostic artifacts to one local folder.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="envelope editorial com 1 ou 2 posts")
    parser.add_argument("--output-dir", type=Path, default=Path("work/localization-harness"))
    parser.add_argument("--api-key", default=os.environ.get("EDITORIAL_API_KEY") or os.environ.get("OPENAI_API_KEY", ""))
    parser.add_argument("--base-url", default=os.environ.get("EDITORIAL_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--model", default=os.environ.get("EDITORIAL_MODEL", "gpt-4o-mini"))
    parser.add_argument("--min-confidence", type=float, default=0.8)
    args = parser.parse_args()

    from unicornio_editor.batch import load_editorial_batch
    from unicornio_editor.content_quality import looks_like_operational_envelope
    from unicornio_editor.editorial_provider import EditorialProviderError, generate_editorial_batch
    from unicornio_editor.editorial_schema import EditorialValidationError, validate_editorial
    from unicornio_editor.language import editorial_language_report

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    input_payload = json.loads(args.input.read_text(encoding="utf-8"))
    _write_json(output_dir / "input.json", input_payload)
    diagnostic: dict[str, Any] = {
        "harness": "editorial_localization_harness",
        "wordpress_access": False,
        "lifecycle_mutation": False,
        "model": args.model,
        "provider_base_url": args.base_url,
    }
    try:
        provider_result = generate_editorial_batch(
            args.input,
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
            min_confidence=args.min_confidence,
            root=output_dir,
            output_path=output_dir / "editorial.output.json",
        )
        batch = load_editorial_batch(output_dir / "editorial.output.json")
        checks: list[dict[str, Any]] = []
        for item in batch["items"]:
            editorial = item.get("editorial") or {}
            item_check: dict[str, Any] = {"post_id": item["post_id"], "status": item["status"]}
            if item["status"] == "ok":
                try:
                    validate_editorial(editorial, min_confidence=args.min_confidence)
                    item_check["schema"] = "pass"
                except (EditorialValidationError, TypeError, ValueError) as exc:
                    item_check["schema"] = "fail"
                    item_check["schema_error"] = str(exc)
                seo = editorial.get("seo") or {}
                content = str(editorial.get("cleaned_html") or "")
                title = str(editorial.get("title") or seo.get("title") or "")
                language = editorial_language_report(
                    title=title,
                    content=content,
                    seo_title=str(seo.get("title") or ""),
                    meta_description=str(seo.get("meta_description") or ""),
                )
                item_check["language"] = language
                item_check["operational_envelope"] = looks_like_operational_envelope(content)
                item_check["facts_review"] = "manual_review_required"
            checks.append(item_check)
        diagnostic.update({
            "status": "ok",
            "provider": provider_result,
            "checks": checks,
            "cost": {
                "input_tokens": provider_result.get("input_tokens", 0),
                "output_tokens": provider_result.get("output_tokens", 0),
            },
        })
        _write_json(output_dir / "diagnostic.json", diagnostic)
        print(json.dumps(diagnostic, ensure_ascii=False, indent=2))
        return 0 if all(item.get("schema") == "pass" and not item.get("operational_envelope") for item in checks if item["status"] == "ok") else 2
    except (EditorialProviderError, OSError, ValueError, json.JSONDecodeError) as exc:
        diagnostic.update({"status": "error", "error": str(exc)})
        _write_json(output_dir / "diagnostic.json", diagnostic)
        print(json.dumps(diagnostic, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
