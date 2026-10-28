from __future__ import annotations

import hashlib
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from PIL import Image, ImageDraw

from unicornio_editor.checklist import run_pre_publish_checklist
from unicornio_editor.config import Config
from unicornio_editor.media.inserter import insert_media
from unicornio_editor.media.source_verify import discover_article_source_candidates
from unicornio_editor.pipeline_v2.model import (
    FeaturedProgress,
    FeaturedStatus,
    InlineMedia,
    LifecycleState,
    MediaProgress,
    Outcome,
    Phase,
    WorkState,
)
from unicornio_editor.pipeline_v2.runtime import WordPressWriterV2
from unicornio_editor.workflow import (
    _execute_media_plan,
    compose_final_content,
    validate_media_plan,
)


pytestmark = pytest.mark.integration


def _artwork(index: int) -> bytes:
    import io

    image = Image.new("RGB", (1600, 900), ((24 + index * 61) % 220, 34, 92 + index * 37))
    draw = ImageDraw.Draw(image)
    # A high-contrast, deterministic mosaic makes the synthetic key art
    # exercise the same anti-flat-image gate as a real detailed asset.
    rng = random.Random(index)
    cell = 32
    for y in range(0, image.height, cell):
        for x in range(0, image.width, cell):
            light = 210 if (x // cell + y // cell + index) % 2 else 35
            tint = rng.randrange(-18, 19)
            color = tuple(max(0, min(255, light + tint + channel)) for channel in (0, 8, -8))
            draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=color)
    for shape in range(1, 9):
        x = (shape * 173 + index * 211) % 1300
        y = (shape * 91 + index * 137) % 650
        color = (
            (31 * shape + index * 43) % 256,
            (71 * shape + index * 29) % 256,
            (113 * shape + index * 17) % 256,
        )
        draw.rectangle((x, y, x + 120 + shape * 7, y + 70 + shape * 5), fill=color)
        draw.ellipse((x // 2, y // 2, x // 2 + 130, y // 2 + 130), outline=(240, 210, 70), width=9)
    draw.polygon([(100, 760), (470, 230 + index * 31), (820, 760)], fill=(218, 184, 104))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class ControlledSource:
    """Local source and OpenAI-compatible vision endpoint; no external network."""

    def __init__(self) -> None:
        self.images = {
            "hollow-knight-silksong-key-art.png": _artwork(1),
            "hollow-knight-silksong-screenshot-one.png": _artwork(2),
            "hollow-knight-silksong-screenshot-two.png": _artwork(3),
        }
        self.operations: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args: Any) -> None:
                return

            def _send(self, status: int, content_type: str, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/article":
                    base = f"http://127.0.0.1:{self.server.server_port}"
                    figures = []
                    for name in owner.images:
                        image_url = f"{base}/images/{name}"
                        subject = "Hollow Knight Silksong"
                        if "key-art" in name:
                            alt = "Hollow Knight Silksong key art"
                            caption = "Arte de Hollow Knight Silksong, por E2E Artist."
                        elif "one" in name:
                            alt = "Hollow Knight Silksong gameplay screenshot one"
                            caption = "Captura de gameplay de Hollow Knight Silksong, por E2E Artist."
                        else:
                            alt = "Hollow Knight Silksong gameplay screenshot two"
                            caption = "Outra captura de gameplay de Hollow Knight Silksong, por E2E Artist."
                        figures.append(
                            f'<figure><img src="{image_url}" alt="{alt}" width="1600" height="900">'
                            f"<figcaption>{caption}</figcaption></figure>"
                        )
                    document = (
                        "<!doctype html><html><head><title>Hollow Knight Silksong imagens</title></head>"
                        "<body><article><h1>Hollow Knight Silksong e o mundo de Pharloom</h1>"
                        + "".join(figures)
                        + "</article></body></html>"
                    ).encode()
                    return self._send(200, "text/html; charset=utf-8", document)
                prefix = "/images/"
                if path.startswith(prefix) and path[len(prefix):] in owner.images:
                    return self._send(200, "image/png", owner.images[path[len(prefix):]])
                self._send(404, "text/plain", b"not found")

            def do_POST(self) -> None:  # noqa: N802
                if urlparse(self.path).path != "/v1/chat/completions":
                    return self._send(404, "text/plain", b"not found")
                size = min(int(self.headers.get("Content-Length") or 0), 20_000_000)
                request = json.loads(self.rfile.read(size).decode("utf-8"))
                messages = request.get("messages") or []
                system = str(messages[0].get("content") or "") if messages else ""
                if "You compare TWO images" in system:
                    owner.operations.append("visual_comparison")
                    answer = {"decision": "DIFFERENT", "confidence": 0.99, "reason": "controlled pixel-distinct E2E artwork"}
                else:
                    owner.operations.append("featured_subject_validation")
                    answer = {
                        "status": "MATCH", "confidence": 0.99, "visual_type": "key_art",
                        "relation": "direct", "editorial_fit": "suitable", "role_fit": "suitable",
                        "reason": "controlled E2E image represents the stated subject",
                    }
                payload = json.dumps({
                    "choices": [{"message": {"content": json.dumps(answer)}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                }).encode()
                self._send(200, "application/json", payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "ControlledSource":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _snapshot(post: dict[str, Any]) -> dict[str, Any]:
    content = post.get("content") if isinstance(post.get("content"), dict) else {}
    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    raw = str(content.get("raw") or content.get("rendered") or "")
    return {
        "post_id": int(post.get("id") or 0),
        "status": str(post.get("status") or ""),
        "featured_media": int(post.get("featured_media") or 0),
        "content_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "ready_state": str(meta.get("_hermes_state") or ""),
        "work_state_sha256": hashlib.sha256(str(meta.get("_hermes_work_state") or "").encode()).hexdigest(),
    }


def test_controlled_source_acquisition_composes_checklists_and_commits_ready_media(wordpress_e2e_case):
    from unicornio_editor.media.registry import record_v2_observation

    case = wordpress_e2e_case
    post_id = case.post_id("media_ready")
    with ControlledSource() as source:
        source_url = f"{source.base_url}/article"
        before_setup = _snapshot(case.client.get_post(post_id))
        case.client.update_post(post_id, {"meta": {"original_link": source_url}})
        post = case.client.get_post(post_id)
        assert post["meta"]["original_link"] == source_url
        before_acquisition = _snapshot(post)

        config = Config(
            content_source="wordpress",
            wordpress_url=case.url,
            wordpress_api_base="/wp-json/wp/v2",
            app_user=case.user,
            app_password=case.password,
            dry_run=False,
            http_timeout=10,
            remote_url_policy="off",
            internal_links_enabled=False,
            vision_enabled=True,
            vision_api_key="e2e-local-only",
            vision_base_url=f"{source.base_url}/v1",
            vision_model="controlled-e2e-pixel-provider",
            vision_mode="all",
            visual_comparison_max_calls=12,
            visual_comparison_min_confidence=0.85,
            max_source_retries=0,
        )
        root = case.root
        source_stats: dict[str, Any] = {}
        candidates = discover_article_source_candidates(
            source_url,
            subject="Hollow Knight Silksong",
            limit=8,
            stats=source_stats,
            url_policy="off",
            root=root,
        )
        assert len(candidates) == 3, source_stats
        assert all(item.get("source_verified") is True for item in candidates)
        by_name = {str(item["direct_image_url"]).rsplit("/", 1)[-1]: item for item in candidates}
        assert set(by_name) == set(source.images)

        title = "Hollow Knight Silksong: imagens mostram o mundo de Pharloom"
        paragraphs = [
            "Hollow Knight Silksong leva a exploração para Pharloom, um reino construído em torno de novas criaturas, caminhos verticais e combates mais ágeis.",
            "A continuação mantém a leitura cuidadosa dos cenários, mas troca o ritmo da jornada e apresenta ferramentas que mudam a forma de enfrentar cada área.",
            "As imagens da fonte mostram ambientes distintos. A primeira destaca a direção de arte, enquanto as capturas seguintes registram momentos diferentes da exploração.",
            "Esse contraste ajuda a entender a variedade visual sem confundir ilustração promocional com captura de gameplay ou repetir o mesmo quadro em tamanhos diferentes.",
            "A página de origem identifica tecnicamente cada arquivo e permite comparar os bytes baixados com os recursos nela listados.",
            "Com a evidência técnica preservada, o artigo pode apresentar a arte principal e duas capturas próprias, mantendo a ligação direta para a fonte consultada.",
        ]
        body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
        featured = {
            **by_name["hollow-knight-silksong-key-art.png"],
            "paragraph_index": 0,
            "is_featured": True,
            "role": "featured",
            "search_query": "Hollow Knight Silksong key art",
        }
        inline_one = {
            **by_name["hollow-knight-silksong-screenshot-one.png"],
            "paragraph_index": 0,
            "is_featured": False,
            "role": "inline",
            "search_query": "Hollow Knight Silksong gameplay screenshot",
        }
        inline_two = {
            **by_name["hollow-knight-silksong-screenshot-two.png"],
            "paragraph_index": 3,
            "is_featured": False,
            "role": "inline",
            "search_query": "Hollow Knight Silksong gameplay screenshot",
        }
        editorial: dict[str, Any] = {
            "site_relevance": {
                "decision": "process", "confidence": 0.99,
                "reason": "controlled E2E source identifies the cited game",
                "matched_topics": ["games"],
            },
            "cleaned_html": body,
            "seo": {
                "title": title,
                "meta_description": "Imagens de Hollow Knight Silksong apresentam Pharloom, a direção de arte e diferentes momentos de exploração com crédito e fonte verificados.",
                "focus_keyword": "Hollow Knight Silksong",
            },
            "media_plan": [featured, inline_one, inline_two],
            "needs_trailer": False,
            "trailer_url": None,
            "game_name": "Hollow Knight Silksong",
            "trailer_unavailable": True,
            "trailer_search_evidence": {
                "query": "Hollow Knight Silksong trailer official",
                "provider": "controlled-e2e-fixture",
                "searched_at": "2026-10-27T00:00:00+00:00",
                "result": "official_not_found",
            },
        }
        preflight = validate_media_plan(
            case.client, editorial, config=config, root=root,
            post_title=title, post_id=post_id,
        )
        assert preflight["valid"] == 3, preflight
        assert preflight["rejected"] == [], preflight
        media_results, featured_id, featured_credit = _execute_media_plan(
            editorial, config, case.client, root,
            preflight=preflight, post_id=post_id, post_title=title,
        )
        accepted = [item for item in media_results if item.get("status") == "accepted"]
        assert len(accepted) == 3, media_results
        assert featured_id is not None
        featured_result = next(item for item in accepted if item.get("featured"))
        inline_results = [item for item in accepted if not item.get("featured")]
        assert len(inline_results) == 2
        for result in accepted:
            attachment = case.client.get_media(int(result["media_id"]))
            details = attachment.get("media_details") or {}
            assert str(attachment.get("source_url") or "").endswith(".webp")
            assert details.get("width", 0) >= 1 and details.get("height", 0) >= 1
            result["media_url"] = str(attachment["source_url"])
            result["width"] = int(details["width"])
            result["height"] = int(details["height"])
        assert (featured_result["width"], featured_result["height"]) == (1280, 720)
        assert all(
            item.get("visual_verification", {}).get("decision") in {"INITIAL", "DIFFERENT"}
            for item in accepted
        )

        inline_plan = [
            {
                "paragraph_index": item["paragraph_index"],
                "media_url": item["media_url"],
                "alt_text": item["alt_text"],
                "credit_text": item["credit_text"],
                "width": item["width"],
                "height": item["height"],
            }
            for item in inline_results
        ]
        body_with_images = insert_media(body, inline_plan)
        schema_media_fields = {
            "paragraph_index", "source_page_url", "direct_image_url", "credit_text", "alt_text",
            "is_featured", "media_library_id", "subject", "search_query",
            "phash", "evidence_score", "evidence_verdict", "candidate_id",
            "decision_id", "decision",
        }
        editorial_with_media = {
            **editorial,
            "cleaned_html": body_with_images,
            "media_plan": [
                {key: value for key, value in item.items() if key in schema_media_fields}
                for item in editorial["media_plan"]
            ],
        }
        content, _trailer, _trailer_status = compose_final_content(
            editorial_with_media, config, source_url, root=root,
        )
        if featured_credit:
            from unicornio_editor.workflow import append_featured_credit

            content = append_featured_credit(content, featured_credit)

        featured_state = FeaturedProgress(
            status=FeaturedStatus.VALID,
            media_id=int(featured_result["media_id"]),
            media_url=str(featured_result["media_url"]),
            sha256=str(featured_result["sha256"]),
            phash=str(featured_result["phash"]),
            visual_group_id=str(featured_result["visual_group_id"]),
            visual_verification=dict(featured_result["visual_verification"]),
            alt_text=str(featured_result["alt_text"]),
            credit_text=str(featured_result["credit_text"]),
            width=int(featured_result["width"]),
            height=int(featured_result["height"]),
        )
        inline_state = tuple(
            InlineMedia(
                media_id=int(item["media_id"]),
                media_url=str(item["media_url"]),
                slot=int(item["paragraph_index"]),
                alt_text=str(item["alt_text"]),
                credit_text=str(item["credit_text"]),
                subject=str(item.get("subject") or "Hollow Knight Silksong"),
                width=int(item["width"]),
                height=int(item["height"]),
                phash=str(item["phash"]),
                sha256=str(item["sha256"]),
                visual_group_id=str(item["visual_group_id"]),
                visual_verification=dict(item["visual_verification"]),
            )
            for item in inline_results
        )
        media_progress = MediaProgress(required=2, inline=inline_state, featured=featured_state)
        media_accounting = media_progress.visual_accounting
        assert media_accounting.valid_distinct_inline_count == 2, media_accounting
        assert media_accounting.unverified_count == 0, media_accounting

        backup = root / "backups" / str(post_id) / "e2e-snapshot.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps(before_acquisition), encoding="utf-8")
        current = case.client.get_post(post_id)
        checklist = run_pre_publish_checklist(
            post={**current, "featured_media": int(featured_id)},
            editorial=editorial_with_media,
            content=content,
            backup_path=backup,
            config=config,
            client=case.client,
            media_context=media_progress.to_dict(),
        )
        failed_checklist_items = [
            item for item in checklist["items"] if item.get("status") != "pass"
        ]
        assert checklist["all_passed"], json.dumps(failed_checklist_items, ensure_ascii=False)
        assert checklist["media_decision"]["accepted"] == 2

        run_id = f"wp-e2e-media-ready-{post_id}"
        work_state = WorkState(
            state=LifecycleState.READY,
            phase=Phase.VALIDATE,
            relevance_approved=True,
            media=media_progress,
        )
        candidate = {
            "_v2_run_id": run_id,
            "title": title,
            "content": content,
            "featured_media": int(featured_id),
            "seo": dict(editorial["seo"]),
            "editorial": editorial_with_media,
            "media": media_progress.to_dict(),
        }
        candidate_path = root / "backups" / str(post_id) / "editorial.candidate.json"
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        candidate_hash = hashlib.sha256(content.encode()).hexdigest()
        (candidate_path.with_name("editorial.validation.json")).write_text(
            json.dumps({
                "_v2_run_id": run_id,
                "candidate_run_id": run_id,
                "candidate_hash": candidate_hash,
                "passed": True,
                "checklist": checklist,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        writer_result = WordPressWriterV2(case.client, root).commit(
            post_id,
            {
                "post": current,
                "v2_run_id": run_id,
                "v2_cycle_id": "e2e-media-ready-cycle",
                "v2_cycle_round": 1,
                "v2_authorization_id": "e2e-media-ready-authorization",
                "original_link": source_url,
            },
            work_state,
            Outcome.ready(),
        )
        assert writer_result["readback"] is True, writer_result
        assert writer_result["media_readback"] is True, writer_result
        assert writer_result["media_application_status"] == "confirmed", writer_result
        after_post = case.client.get_post(post_id)
        after = _snapshot(after_post)
        assert after["status"] == "pending"
        assert after["ready_state"] == "ready"
        assert after["featured_media"] == int(featured_id)
        for item in inline_state:
            assert item.media_url in str((after_post.get("content") or {}).get("raw") or "")

        record_v2_observation(
            root,
            post_id=post_id,
            run_id=run_id,
            state=work_state,
            phase="validate",
            outcome="ready",
            operation_status="completed",
            write_started=True,
            readback_confirmed=True,
        )
        audit = case._control(f"/wp-json/unicornio-e2e/v1/audit?post_id={post_id}")
        case.observations.append({
            "scenario": "media_to_ready",
            "post_id": post_id,
            "run_id": run_id,
            "before": before_setup,
            "before_acquisition": before_acquisition,
            "after": after,
            "source": {
                "status": source_stats.get("source_status"),
                "candidate_count": len(candidates),
                "source_verified_count": sum(bool(item.get("source_verified")) for item in candidates),
                "evidence_scores": [int(item.get("evidence_score") or 0) for item in candidates],
            },
            "acquisition": {
                "accepted": len(accepted),
                "featured_media_id": int(featured_id),
                "inline_media_ids": [int(item.media_id) for item in inline_state],
                "webp_dimensions": [[int(item["width"]), int(item["height"])] for item in accepted],
                "visual_decisions": [item.get("visual_verification", {}).get("decision") for item in accepted],
                "controlled_vision_operations": list(source.operations),
            },
            "checklist": {
                "all_passed": bool(checklist["all_passed"]),
                "passed": checklist["passed"],
                "failed": checklist["failed"],
                "media_decision": checklist["media_decision"],
            },
            "writer_result": writer_result,
            "journal": case._journal_summary(post_id),
            "registry_v5": case._registry_summary(run_id),
            "rest_events": audit.get("events", []),
        })
