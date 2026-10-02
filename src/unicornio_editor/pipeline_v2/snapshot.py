"""Read-only production snapshot capture and offline V2 conversion."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .legacy import LegacyStateLoader
from .model import FeaturedStatus
from .scheduler import next_action
from .shadow import V1_TO_V2_LIFECYCLE, compare_work_state


def capture_snapshot(post_id: int, post_reader: Callable[[int], dict[str, Any]], manifest_reader: Callable[[int], dict[str, Any]], output_dir: str | Path) -> Path:
    """Perform only injected reads and write a local immutable snapshot."""
    post = post_reader(post_id)
    manifest = manifest_reader(post_id)
    payload = {"post_id": post_id, "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "wp": {"status": post.get("status"), "meta": post.get("meta", {}), "context": {"id": post.get("id"), "type": post.get("type"), "slug": post.get("slug"), "link": post.get("link"), "title": post.get("title", {}), "content": post.get("content", {}), "excerpt": post.get("excerpt", {}), "featured_media": post.get("featured_media")}}, "manifest": manifest}
    target = Path(output_dir) / f"{post_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return target



def expected_from_v1_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build the shadow oracle from V1 fields only, never from WorkState."""
    wp = snapshot.get("wp", {})
    meta = wp.get("meta", {}) or {}
    manifest = snapshot.get("manifest", {}) or {}
    v1_state = meta.get("_hermes_state") or "new"
    required = int(meta.get("_hermes_media_required") or 0)
    completed = int(meta.get("_hermes_media_completed") or 0)
    missing = int(meta.get("_hermes_media_missing") or max(0, required - completed))
    last_error = str(meta.get("_hermes_last_error") or "").casefold()
    kind = str(meta.get("_hermes_partial_kind") or "").casefold()
    blocked_markers = (
        (("qualidade_texto",), "text_quality"),
        (("seo",), "seo"),
        (("estrutura",), "structure"),
        (("schema",), "schema"),
        (("destaque",), "featured_invalid"),
        (("imagens_visao", "imagens visão", "featured vision"), "featured_vision"),
        (("imagens_no_corpo",), "inline_missing"),
        (("imagens_webp",), "media_invalid"),
        (("fonte",), "source"),
        (("trailer",), "trailer"),
    )
    blocked_marker = next((blocker for markers, blocker in blocked_markers if any(marker in last_error for marker in markers)), None)
    if kind == "media" and missing == 0 and completed == required and blocked_marker == "featured_vision":
        blocker = "featured_vision"
    elif kind == "featured_vision":
        blocker = "featured_vision"
    elif kind == "featured_missing":
        blocker = "featured_missing"
    elif kind == "inline_missing" or v1_state == "partial":
        blocker = "inline_missing"
    elif v1_state == "blocked" and blocked_marker:
        blocker = blocked_marker
    else:
        blocker = None
    if v1_state == "partial":
        phase = "media"
    elif v1_state == "blocked":
        phase = "media" if blocker in {"featured_invalid", "featured_vision", "inline_missing", "media_invalid"} else "editorial"
    else:
        phase = "validate" if v1_state == "ready" else "relevance"
    assets = manifest.get("accepted_media", []) or [] if v1_state in {"partial", "blocked", "uncertain"} else []
    ids = [int(item["media_id"]) for item in assets]
    slots = [int(item.get("slot", item.get("paragraph_index", i + 1))) for i, item in enumerate(assets)]
    featured = manifest.get("featured") if isinstance(manifest.get("featured"), dict) else {}
    featured_status = {"rejected": "vision_rejected", "vision": "vision_rejected", "failed": "invalid"}.get(str(featured.get("status", "missing")), str(featured.get("status", "missing")))
    lifecycle = V1_TO_V2_LIFECYCLE.get(v1_state, "pending")
    if lifecycle != "pending":
        phase = {"ready": "validate", "published": "publish", "skipped": "relevance"}.get(lifecycle)
        blocker = None
        required = completed = missing = 0
        ids = slots = []
        featured_status = "missing"
        action = "none"
    elif phase == "relevance":
        action = "evaluate_relevance"
    elif phase == "editorial":
        action = "regenerate_editorial"
    elif missing > 0:
        action = "resolve_inline"
    elif featured_status != "valid":
        action = "resolve_featured"
    else:
        action = "validate"
    return {"lifecycle": lifecycle, "phase": phase, "blocker": blocker, "required": required, "accepted": completed, "missing": missing, "media_ids": ids, "slots": slots, "featured": featured_status, "next_action": action}


def compare_snapshot(path: str | Path) -> dict[str, Any]:
    """Convert one snapshot offline; does not call WordPress or persist state."""
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    post_id = int(snapshot["post_id"])
    wp = snapshot.get("wp", {})
    manifest = snapshot.get("manifest", {})
    loader = LegacyStateLoader(lambda _: manifest)
    state = loader.load(post_id, wp.get("meta", {}))
    oracle = expected_from_v1_snapshot(snapshot)
    v1_state = (wp.get("meta", {}) or {}).get("_hermes_state") or "new"
    action = next_action(state)
    expected = {"required": oracle["required"], "accepted": oracle["accepted"], "missing": oracle["missing"], "blocker": oracle["blocker"], "phase": oracle["phase"], "next_action": oracle["next_action"]}
    report = compare_work_state(post_id, v1_state, state, expected=expected, expected_ids=oracle["media_ids"], expected_slots=oracle["slots"], expected_action=oracle["next_action"], actual_action=action, expected_featured=oracle["featured"])
    report["snapshot"] = str(path)
    report["production_writes"] = 0
    report["wordpress_writes"] = 0
    return report
