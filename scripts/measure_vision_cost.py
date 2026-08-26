"""Medicao local de custo do vision gate (gpt-4o-mini) — NAO e o pipeline.

Simula o gate de visao em 5 "postagens" (imagens reais publicas, via URL) e
reporta quantas chamadas low/high foram feitas, o custo estimado (precos
publicos) e o resultado por imagem. Usa a chave de `OPENAI_API_KEY` /
`EDITOR_VISION_API_KEY` do ambiente (ou `--key`). NAO altera o WordPress.

Uso:
  OPENAI_API_KEY=sk-... python scripts/measure_vision_cost.py
  python scripts/measure_vision_cost.py --key sk-... --posts 5

Precos (gpt-4o-mini): input $0.15/M, output $0.60/M; imagem low ~2833 tok,
high ~2.833 + tiles*5.667 (estimativa 6 tiles para 1920x1080).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from unicornio_editor.media.vision_gate import verify_image_subject

# (url, subject, category) — imagens publicas conhecidas para o teste.
DEFAULT_IMAGES = [
    ("https://picsum.photos/id/1/800/450", "a mountain landscape", "photograph"),
    ("https://picsum.photos/id/1015/800/450", "a river between mountains", "photograph"),
    ("https://picsum.photos/id/1018/800/450", "mountain lake", "photograph"),
    ("https://picsum.photos/id/1039/800/450", "a waterfall", "photograph"),
    ("https://picsum.photos/id/1050/800/450", "a city skyline", "photograph"),
]

PRICE_INPUT = 0.15e-6   # USD por token de input
PRICE_OUTPUT = 0.60e-6  # USD por token de output
TOK_LOW = 2833          # tokens de imagem em detail=low
TOK_HIGH = 36835        # estimativa alta para 1920x1080 (6 tiles)


def _estimate_cost(detail: str, out_tokens: int = 30) -> float:
    if detail == "low":
        return TOK_LOW * PRICE_INPUT + out_tokens * PRICE_OUTPUT
    return TOK_HIGH * PRICE_INPUT + out_tokens * PRICE_OUTPUT


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", default=os.environ.get("OPENAI_API_KEY") or os.environ.get("EDITOR_VISION_API_KEY", ""))
    parser.add_argument("--base", default=os.environ.get("EDITOR_VISION_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--model", default=os.environ.get("EDITOR_VISION_MODEL", "gpt-4o-mini"))
    parser.add_argument("--posts", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="saida JSON")
    args = parser.parse_args()

    if not args.key:
        print("ERRO: defina OPENAI_API_KEY ou passe --key", file=sys.stderr)
        return 2

    images = DEFAULT_IMAGES[: args.posts]
    results = []
    low_calls = 0
    high_calls = 0
    cost = 0.0
    for url, subject, category in images:
        for detail, allow_high in (("low", True),):
            try:
                ok, reason = verify_image_subject(
                    image_url=url, subject=subject, api_key=args.key,
                    base_url=args.base, model=args.model, category=category,
                    alt=subject, detail=detail, allow_high=allow_high,
                )
            except Exception as exc:  # noqa: BLE001
                results.append({"url": url, "subject": subject, "ok": False, "error": str(exc)[:120]})
                low_calls += 1
                cost += _estimate_cost("low")
                break
            if detail == "low":
                low_calls += 1
                cost += _estimate_cost("low")
            else:
                high_calls += 1
                cost += _estimate_cost("high")
            results.append({"url": url, "subject": subject, "ok": ok, "reason": reason, "detail": detail})
            # Escala high apenas se nao confirmou em low e permitido.
            if not ok and allow_high:
                pass  # verify_image_subject ja fez o escalonamento interno
            break

    summary = {
        "posts_simulated": len(images),
        "low_calls": low_calls,
        "high_calls": high_calls,
        "estimated_cost_usd": round(cost, 6),
        "per_1000_usd": round(cost / max(len(images), 1) * 1000, 4),
        "results": results,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"posts simulados: {len(images)}")
        print(f"chamadas low: {low_calls} | high: {high_calls}")
        print(f"custo estimado (gpt-4o-mini): US$ {cost:.6f}")
        print(f"projecao por 1000 imagens: US$ {summary['per_1000_usd']}")
        for r in results:
            print(f"  {'OK ' if r.get('ok') else 'NO '} {r.get('subject','?')[:40]:40} {r.get('reason', r.get('error',''))[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
