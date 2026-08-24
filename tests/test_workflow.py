import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unicornio_editor.config import Config
from unicornio_editor.workflow import (
    apply_editorial,
    build_cards,
    build_queue_report,
    get_cleaned_content,
    prepare_post,
    publish_post,
    publish_ready_posts,
    validate_media_plan,
)


def editorial_payload(decision="process"):
    # Politica de imagens (2/4/6 sem waiver): o payload de teste reflete um
    # editorial valido — 2 imagens reais com credito + keyword no corpo.
    title = "Título sobre videogame e lançamento importante"
    images = (
        '<figure><img src="https://s3.example/noticia-importante-1.webp" alt="%s" width="800" height="450" />'
        "<figcaption>Crédito: Autor. Licença CC BY 4.0 (https://creativecommons.org/licenses/by/4.0).</figcaption></figure>"
        '<figure><img src="https://s3.example/noticia-importante-2.webp" alt="%s" width="800" height="450" />'
        "<figcaption>Crédito: Autor. Licença CC BY 4.0 (https://creativecommons.org/licenses/by/4.0).</figcaption></figure>"
    ) % (title, title)
    return {
        "site_relevance": {
            "decision": decision,
            "confidence": 0.99,
            "reason": "Teste",
            "matched_topics": ["games"] if decision == "process" else [],
        },
        "cleaned_html": f"<p>Texto revisado sobre videogame e lançamento.</p>{images}",
        "seo": {
            "title": title,
            "meta_description": "Uma descrição suficientemente longa sobre o conteúdo de videogame, seus detalhes, plataformas e contexto para o leitor entender a notícia.",
            "focus_keyword": "videogame",
        },
        "media_plan": [],
        "needs_trailer": False,
        "trailer_url": None,
        "game_name": None,
    }


class FakeClient:
    def __init__(self, post):
        self.post = post
        self.updated = []

    def get_post(self, post_id):
        return self.post

    def list_pending(self, **kwargs):
        return [self.post]

    def get_media(self, media_id):
        # Post-fix behavior: re-uploaded featured keeps provenance evidence
        # in its filename/title/alt (no more generic "featured.webp"). The
        # slug derives from the ORIGINAL source file name, which carries a
        # distinctive entity of the post ("importante" here).
        return {
            "id": media_id,
            "source_url": "https://wp.test/uploads/noticia-sobre-videogame-e-lancamento-importante-1280x720.webp",
            "title": {"rendered": "Notícia sobre videogame e lançamento importante"},
            "alt_text": "Notícia sobre videogame e lançamento importante",
            "media_details": {"width": 1280, "height": 720},
        }

    def update_post(self, post_id, payload):
        self.updated.append((post_id, payload))
        return {"id": post_id, "status": "pending", **payload}

    def publish(self, post_id, meta=None):
        payload = {"status": "publish"}
        if meta:
            payload["meta"] = meta
        self.updated.append((post_id, payload))
        return {
            "id": post_id,
            "status": "publish",
            "link": f"https://wp.test/?p={post_id}",
            **payload,
        }


class WorkflowTests(unittest.TestCase):
    def config(self, dry_run):
        return Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=dry_run)

    def post(self):
        return {
            "id": 42,
            "status": "pending",
            "date": "2026-08-21T03:00:00",
            "date_gmt": "2026-08-21T06:00:00",
            "content": {"raw": "<article><p>Original.</p></article>"},
            "meta": {"original_link": "https://source.example/news"},
            "featured_media": 7,
        }

    def test_prepare_creates_snapshot_and_cleans_content(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            report = prepare_post(client, Path(directory), 42)
            self.assertEqual(report["post_id"], 42)
            self.assertEqual(report["cleaned_html"], "<p>Original.</p>")
            self.assertTrue(Path(report["backup"]).exists())
            self.assertEqual(client.updated, [])

    def test_apply_without_cleaned_html_reuses_prepared_content(self):
        # No-rewrite path (token economy): the model omits cleaned_html and
        # apply must deterministically reuse the cleaned post content,
        # then compose the canonical CTA + Fonte in code.
        with tempfile.TemporaryDirectory() as directory:
            payload = editorial_payload()
            del payload["cleaned_html"]
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(True), Path(directory), 42, payload)
            self.assertIn("<p>Original.</p>", report["content_preview"])
            self.assertIn("Confira mais novidades", report["content_preview"])
            self.assertIn('Fonte: <a href="https://source.example/news"', report["content_preview"])
            self.assertEqual(report["wordpress_changed"], False)

    def test_queue_reports_pending_and_edited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            report = build_queue_report(client, root)
            self.assertEqual(report["pending"], 1)
            self.assertEqual(report["edited"], 0)
            self.assertEqual(report["unprocessed_ids"], [42])
            self.assertEqual(report["recent_unprocessed_ids"], [42])
            # After apply, the post counts as edited and leaves the monitor line.
            apply_editorial(client, self.config(False), root, 42, editorial_payload())
            report = build_queue_report(client, root)
            self.assertEqual(report["edited"], 1)
            self.assertEqual(report["unprocessed_ids"], [])
            self.assertEqual(report["recent_unprocessed_ids"], [])

    def test_queue_reports_blocked_as_rework_not_edited(self):
        # Fix do loop verificar->corrigir->publicar: post com
        # editorial.blocked.json (publish gate reabriu, ou apply recusou) NAO
        # conta como "edited" (pronto p/ publicar) — conta como blocked/rework e
        # entra na linha do monitor para o agente editorial acordar e corrigir.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backups" / "42").mkdir(parents=True)
            payload = editorial_payload()
            (root / "backups" / "42" / "editorial.latest.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            (root / "backups" / "42" / "editorial.blocked.json").write_text(
                json.dumps(
                    {"status": "blocked", "reason": "checklist pre-publicacao com falhas"}
                ),
                encoding="utf-8",
            )
            client = FakeClient(self.post())
            report = build_queue_report(client, root)
            self.assertEqual(report["edited"], 0)
            self.assertEqual(report["blocked"], 1)
            self.assertEqual(report["unprocessed_ids"], [])
            self.assertEqual(report["blocked_ids"], [42])
            self.assertEqual(report["recent_blocked_ids"], [42])
            # Sem latest.json (apply recusou por imagens_no_corpo): continua
            # rework, nunca volta como "unprocessed" sem marcador.
            (root / "backups" / "42" / "editorial.latest.json").unlink()
            report = build_queue_report(client, root)
            self.assertEqual(report["blocked"], 1)
            self.assertEqual(report["unprocessed_ids"], [])
            self.assertEqual(report["recent_blocked_ids"], [42])
            # uncertain vence: se o agente ja registrou uncertain.json, o post
            # sai da fila de trabalho — re-tentar so queimaria tokens.
            (root / "backups" / "42" / "uncertain.json").write_text(
                json.dumps({"status": "uncertain"}), encoding="utf-8"
            )
            report = build_queue_report(client, root)
            self.assertEqual(report["blocked"], 0)
            self.assertEqual(report["uncertain"], 1)
            self.assertEqual(report["blocked_ids"], [])

    def test_queue_monitor_excludes_old_backlog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = self.post()
            old["date"] = "2026-05-06T10:00:00"
            old["date_gmt"] = "2026-05-06T13:00:00"
            report = build_queue_report(FakeClient(old), root)
            self.assertEqual(report["unprocessed_ids"], [42])
            self.assertEqual(report["recent_unprocessed_ids"], [])

    def test_apply_inherits_valid_seo_from_post_meta(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = editorial_payload()
            del payload["seo"]
            post = self.post()
            post["meta"] = {
                "original_link": "https://source.example/news",
                "rank_math_title": "Titulo herdado do post",
                "rank_math_description": "Uma descricao suficientemente longa e detalhada para o SEO herdado do post de videogame, valida dentro dos limites exigidos pelo portal editorial.",
                "rank_math_focus_keyword": "videogame",
            }
            client = FakeClient(post)
            report = apply_editorial(client, self.config(True), Path(directory), 42, payload)
            saved = json.loads(
                Path(directory, "backups/42/editorial.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["seo"]["title"], "Titulo herdado do post")
            self.assertIn("<p>Texto revisado sobre videogame e lançamento.</p>", report["content_preview"])

    def test_apply_requires_seo_when_post_meta_invalid(self):
        from unicornio_editor.editorial_schema import EditorialValidationError

        with tempfile.TemporaryDirectory() as directory:
            payload = editorial_payload()
            del payload["seo"]
            client = FakeClient(self.post())  # meta sem rank_math
            with self.assertRaises(EditorialValidationError):
                apply_editorial(client, self.config(True), Path(directory), 42, payload)

    def test_apply_low_confidence_skip_marks_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = editorial_payload("skip")
            payload["site_relevance"]["confidence"] = 0.70
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
            self.assertEqual(report["status"], "uncertain")
            self.assertFalse(report["wordpress_changed"])
            self.assertTrue(Path(directory, "backups/42/uncertain.json").is_file())
            self.assertFalse(Path(directory, "backups/42/editorial.latest.json").exists())

    def test_apply_confident_skip_is_final(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = editorial_payload("skip")
            payload["site_relevance"]["confidence"] = 0.99
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
            self.assertFalse(report["wordpress_changed"])
            self.assertIn("skip_reason", report)
            self.assertFalse(Path(directory, "backups/42/uncertain.json").exists())
            self.assertTrue(Path(directory, "backups/42/editorial.latest.json").is_file())

    def test_build_cards_reports_gaps_and_entities(self):
        with tempfile.TemporaryDirectory() as directory:
            post = self.post()
            post["title"] = {"raw": "Redfall ganha novo gameplay"}
            post["content"] = {
                "raw": (
                    '<figure><img src="https://s3.example/r.webp" alt="Redfall key art" />'
                    "<figcaption>Crédito da imagem: Autor. Redfall. Licença CC BY 4.0 "
                    "(https://creativecommons.org/licenses/by/4.0).</figcaption></figure>"
                    "<p>Redfall chega com vampiros.</p>"
                )
            }
            post["featured_media"] = 7
            post["meta"] = {
                "original_link": "https://source.example/news",
                "rank_math_title": "Redfall ganha gameplay",
                "rank_math_description": "Uma descricao suficientemente longa e detalhada para o SEO do post sobre o jogo Redfall, valida dentro dos limites exigidos pelo portal editorial.",
                "rank_math_focus_keyword": "redfall",
            }
            client = FakeClient(post)
            report = build_cards(client, self.config(True), Path(directory))
            card = report["cards"][0]
            self.assertEqual(card["id"], 42)
            self.assertIn("redfall", card["entities"])
            self.assertTrue(card["featured"])
            self.assertTrue(card["seo_exists"])
            self.assertTrue(card["game_hint"])
            self.assertEqual(card["images"]["total"], 1)
            self.assertEqual(card["images"]["relevantes"], 1)
            self.assertEqual(card["images"]["preservadas"], 1)
            self.assertEqual(card["original_link"], "https://source.example/news")

    def test_build_cards_marks_blocked_with_reason_and_sorts_first(self):
        # Fix do loop: card de post reaberto pelo publish gate mostra
        # blocked=true + blocked_reason (o que corrigir) e vem PRIMEIRO no
        # lote — o agente editorial corrige rework antes de posts novos.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked_post = self.post()  # id 42
            blocked_post["title"] = {"raw": "Jogo bloqueado no gate"}
            new_post = dict(blocked_post)
            new_post["id"] = 43
            new_post["title"] = {"raw": "Post novo qualquer"}
            (root / "backups" / "42").mkdir(parents=True)
            (root / "backups" / "42" / "editorial.latest.json").write_text(
                json.dumps(editorial_payload()), encoding="utf-8"
            )
            (root / "backups" / "42" / "editorial.blocked.json").write_text(
                json.dumps(
                    {
                        "post_id": 42,
                        "status": "blocked",
                        "blocked_checklist": {
                            "items": [
                                {"name": "imagens_no_corpo", "status": "fail"},
                                {"name": "destaque_1280x720", "status": "fail"},
                                {"name": "backup", "status": "pass"},
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            class FakeMulti(FakeClient):
                def list_pending(self, **kwargs):
                    return [blocked_post, new_post]

            report = build_cards(FakeMulti(blocked_post), self.config(True), root)
            cards = report["cards"]
            self.assertEqual([c["id"] for c in cards], [42, 43])  # rework primeiro
            self.assertTrue(cards[0]["blocked"])
            self.assertFalse(cards[0]["edited"])  # latest existe, mas blocked
            self.assertEqual(
                cards[0]["blocked_reason"],
                "checklist: imagens_no_corpo, destaque_1280x720",
            )
            self.assertFalse(cards[1]["blocked"])
            self.assertIsNone(cards[1]["blocked_reason"])

    def test_build_cards_fetches_rework_by_include_and_fills_with_new(self):
        # P2: o cards NAO carrega 100 posts — rework vem do filesystem e e
        # buscado por include; novos so para completar o lote (per_page=batch).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked_post = self.post()
            blocked_post["title"] = {"raw": "Rework antigo"}
            new_post = dict(blocked_post)
            new_post["id"] = 43
            new_post["title"] = {"raw": "Novo post"}
            (root / "backups" / "42").mkdir(parents=True)
            (root / "backups" / "42" / "editorial.blocked.json").write_text(
                json.dumps({"status": "blocked"}), encoding="utf-8"
            )

            class RecordingClient(FakeClient):
                def __init__(self, posts):
                    self.posts = posts
                    self.calls = []
                    self.seen = set()

                def list_pending(self, **kwargs):
                    self.calls.append(kwargs)
                    include = kwargs.get("include")
                    candidates = [
                        p
                        for p in self.posts
                        if p["id"] not in self.seen
                        and (p["id"] in include if include else True)
                    ]
                    result = candidates[: kwargs.get("per_page", 10)]
                    self.seen.update(p["id"] for p in result)
                    return result

            client = RecordingClient([blocked_post, new_post])
            report = build_cards(client, self.config(True), root)
            self.assertEqual([c["id"] for c in report["cards"]], [42, 43])
            self.assertEqual(client.calls[0]["include"], [42])  # rework por include
            self.assertEqual(client.calls[1]["per_page"], 1)  # 1 novo completa o lote

    def test_get_cleaned_content_returns_cleaned_html(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            result = get_cleaned_content(client, Path(directory), 42)
            self.assertEqual(result["post_id"], 42)
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["cleaned_html"], "<p>Original.</p>")
            self.assertEqual(result["original_link"], "https://source.example/news")

    def test_validate_media_plan_rejects_irrelevant_items(self):
        payload = editorial_payload()
        payload["media_plan"] = [
            self.media_item(paragraph_index=0),
            {
                **self.media_item(paragraph_index=3),
                "alt_text": "gatinho fofo dormindo",
                "direct_image_url": "https://s3.example/gatinho.jpg",
                "source_page_url": "https://s3.example/gatinho",
                "credit_text": "Crédito da imagem: Autor. CC0.",
            },
        ]
        client = FakeClient(self.post())
        result = validate_media_plan(client, payload)
        self.assertEqual(result["valid"], 1)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(result["rejected"][0]["index"], 1)
        self.assertIn("sem relacao", result["rejected"][0]["reason"])

    def test_apply_skip_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload("skip"))
            self.assertFalse(report["wordpress_changed"])
            self.assertEqual(client.updated, [])

    def test_apply_processes_pending_without_sending_status(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload())
            self.assertTrue(report["wordpress_changed"])
            self.assertNotIn("status", client.updated[0][1])
            self.assertIn("Portal de", client.updated[0][1]["content"]["raw"])
            self.assertEqual(
                client.updated[0][1]["meta"]["rank_math_focus_keyword"], "videogame"
            )
            self.assertEqual(client.updated[0][1]["meta"]["_ai_editor_decision"], "process")

    def test_apply_clears_blocked_and_uncertain_markers_on_success(self):
        # Fix do loop: re-aplicar com sucesso remove editorial.blocked.json e
        # uncertain.json — sem isso o post ficaria "blocked" para sempre e o
        # monitor acordaria o agente em loop corrigindo post já corrigido.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backups" / "42").mkdir(parents=True)
            (root / "backups" / "42" / "editorial.blocked.json").write_text(
                json.dumps({"status": "blocked"}), encoding="utf-8"
            )
            (root / "backups" / "42" / "uncertain.json").write_text(
                json.dumps({"status": "uncertain"}), encoding="utf-8"
            )
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), root, 42, editorial_payload())
            self.assertTrue(report["wordpress_changed"])
            self.assertFalse((root / "backups/42/editorial.blocked.json").exists())
            self.assertFalse((root / "backups/42/uncertain.json").exists())
            queue = build_queue_report(client, root)
            self.assertEqual(queue["blocked"], 0)
            self.assertEqual(queue["edited"], 1)

    def test_apply_embeds_youtube_trailer_for_game_content(self):
        trailer = {
            "video_id": "abcDEF12345",
            "title": "Hellraiser: Revival - Official Trailer",
            "author_name": "Boss Team Games",
            "author_url": "https://www.youtube.com/@BossTeamGames",
            "watch_url": "https://www.youtube.com/watch?v=abcDEF12345",
            "embed_url": "https://www.youtube-nocookie.com/embed/abcDEF12345",
            "thumbnail_url": "https://i.ytimg.com/vi/abcDEF12345/hqdefault.jpg",
            "matched_title": "Hellraiser: Revival - Official Trailer",
        }
        payload = editorial_payload()
        payload["game_name"] = "Hellraiser: Revival"
        with mock.patch("unicornio_editor.workflow.find_game_trailer", return_value=trailer):
            with tempfile.TemporaryDirectory() as directory:
                client = FakeClient(self.post())
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        raw = client.updated[0][1]["content"]["raw"]
        self.assertIn("https://www.youtube-nocookie.com/embed/abcDEF12345", raw)
        self.assertIn("Confira mais novidades", raw)
        self.assertLess(raw.index("youtube-nocookie"), raw.index("Confira mais novidades"))
        self.assertEqual(report["trailer"]["video_id"], "abcDEF12345")

    def test_apply_without_game_name_does_not_search(self):
        with mock.patch("unicornio_editor.workflow.find_game_trailer") as discovery:
            with tempfile.TemporaryDirectory() as directory:
                client = FakeClient(self.post())
                report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload())
        discovery.assert_not_called()
        self.assertIsNone(report["trailer"])

    def test_apply_dry_run_reports_trailer_in_preview(self):
        trailer = {
            "video_id": "abcDEF12345",
            "title": "Hellraiser: Revival - Official Trailer",
            "author_name": "Boss Team Games",
            "author_url": "https://www.youtube.com/@BossTeamGames",
            "watch_url": "https://www.youtube.com/watch?v=abcDEF12345",
            "embed_url": "https://www.youtube-nocookie.com/embed/abcDEF12345",
            "thumbnail_url": "https://i.ytimg.com/vi/abcDEF12345/hqdefault.jpg",
            "matched_title": "Hellraiser: Revival - Official Trailer",
        }
        payload = editorial_payload()
        payload["game_name"] = "Hellraiser: Revival"
        with mock.patch("unicornio_editor.workflow.find_game_trailer", return_value=trailer):
            with tempfile.TemporaryDirectory() as directory:
                client = FakeClient(self.post())
                report = apply_editorial(client, self.config(True), Path(directory), 42, payload)
        self.assertFalse(report["wordpress_changed"])
        self.assertIn("youtube-nocookie.com/embed/abcDEF12345", report["content_preview"])
        self.assertEqual(report["trailer"]["video_id"], "abcDEF12345")
        self.assertEqual(client.updated, [])

    @staticmethod
    def media_item(paragraph_index=0, is_featured=False):
        return {
            "paragraph_index": paragraph_index,
            "source_page_url": "https://source.example/titulo",
            "direct_image_url": "https://source.example/titulo.jpg",
            "author": "Autor",
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "captured_at": "2026-08-20T12:00:00Z",
            "credit_text": "Crédito da imagem: Autor. Imagem do titulo do jogo. Licença CC BY 4.0 (https://creativecommons.org/licenses/by/4.0).",
            "alt_text": "Imagem do titulo do jogo",
            "is_featured": is_featured,
        }

    def test_apply_executes_media_plan_and_sets_featured(self):
        payload = editorial_payload()
        payload["cleaned_html"] = "<p>Um.</p><p>Dois.</p><p>Tres.</p><p>Quatro.</p><p>Cinco.</p><p>Seis.</p><p>Sete.</p>"
        payload["media_plan"] = [
            self.media_item(paragraph_index=0),
            self.media_item(paragraph_index=3),
            self.media_item(paragraph_index=6, is_featured=True),
        ]
        with mock.patch("unicornio_editor.workflow.download_image", return_value=Path("/tmp/source.jpg")), mock.patch(
            "unicornio_editor.workflow.convert_to_webp", return_value=Path("/tmp/inline.webp")
        ), mock.patch(
            "unicornio_editor.workflow.prepare_featured_webp", return_value=Path("/tmp/featured.webp")
        ), mock.patch(
            "unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "teste")
        ), mock.patch(
            "unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)
        ), mock.patch(
            "unicornio_editor.workflow.upload_image",
            side_effect=[
                {"id": 50, "source_url": "https://wp.test/50.webp"},
                {"id": 51, "source_url": "https://wp.test/51.webp"},
                {"id": 52, "source_url": "https://wp.test/52.webp"},
            ],
        ):
            with tempfile.TemporaryDirectory() as directory:
                client = FakeClient(self.post())
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        payload_sent = client.updated[0][1]
        self.assertEqual(payload_sent["featured_media"], 52)
        raw = payload_sent["content"]["raw"]
        self.assertIn("https://wp.test/50.webp", raw)
        self.assertIn("https://wp.test/51.webp", raw)
        self.assertIn("Crédito da imagem", raw)
        self.assertEqual(report["featured_media"], 52)
        self.assertEqual(len(report["media_plan_results"]), 3)
        self.assertTrue(report["media_plan_results"][2]["featured"])

    def test_apply_dry_run_blocks_media_plan(self):
        payload = editorial_payload()
        payload["media_plan"] = [self.media_item(paragraph_index=1, is_featured=True)]
        with mock.patch("unicornio_editor.workflow.download_image") as download:
            with tempfile.TemporaryDirectory() as directory:
                client = FakeClient(self.post())
                report = apply_editorial(client, self.config(True), Path(directory), 42, payload)
        download.assert_not_called()
        self.assertEqual(report["media_plan_results"][0]["status"], "blocked")
        self.assertFalse(report["wordpress_changed"])
        self.assertEqual(client.updated, [])

    def test_apply_normalizes_existing_featured_to_1280x720(self):
        post = self.post()
        post["featured_media"] = 7
        # Existing featured whose REAL evidence references a cited work of the
        # editorial ("lancamento importante" from the SEO title) is reused.
        media = {
            "id": 7,
            "source_url": "https://wp.test/uploads/noticia-sobre-videogame-e-lancamento-importante.jpg",
            "media_details": {"width": 2560, "height": 1916},
            "alt_text": "Notícia sobre videogame e lançamento importante",
            "title": {"rendered": "Notícia sobre videogame e lançamento importante"},
            "caption": {"rendered": "Crédito da imagem: Autor."},
        }

        class MediaClient(FakeClient):
            def get_media(self, media_id):
                return media

            def upload_media(self, path, *, filename, alt_text, title, caption=None):
                self.uploads = getattr(self, "uploads", 0) + 1
                return {"id": 88, "source_url": "https://wp.test/uploads/featured-1280x720.webp"}

        with mock.patch("unicornio_editor.workflow.download_image", return_value=Path("/tmp/old.jpg")), mock.patch(
            "unicornio_editor.workflow.prepare_featured_webp", return_value=Path("/tmp/new.webp")
        ):
            with tempfile.TemporaryDirectory() as directory:
                client = MediaClient(post)
                report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload())
        self.assertEqual(client.uploads, 1)
        self.assertEqual(client.updated[0][1]["featured_media"], 88)
        self.assertEqual(report["featured_media"], 88)

    def test_apply_does_not_reuse_generic_existing_featured(self):
        # A generic article header/wordmark (no cited work in its real
        # evidence) must NOT be reused as featured — the post stays without
        # a featured so the flow is forced to supply a real key art.
        post = self.post()
        post["featured_media"] = 7
        media = {
            "id": 7,
            "source_url": "https://wp.test/uploads/5-classic-anime-banner.jpg",
            "media_details": {"width": 1200, "height": 675},
            "alt_text": "5 Classic Anime That Deserve Remakes",
            "title": {"rendered": "5 Classic Anime That Deserve Remakes"},
        }

        class GenericMediaClient(FakeClient):
            def get_media(self, media_id):
                return media

            def upload_media(self, path, *, filename, alt_text, title, caption=None):
                self.uploads = getattr(self, "uploads", 0) + 1
                return {"id": 88, "source_url": "https://wp.test/uploads/new.webp"}

        with mock.patch("unicornio_editor.workflow.download_image", return_value=Path("/tmp/old.jpg")), mock.patch(
            "unicornio_editor.workflow.prepare_featured_webp", return_value=Path("/tmp/new.webp")
        ):
            with tempfile.TemporaryDirectory() as directory:
                client = GenericMediaClient(post)
                report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload())
        self.assertEqual(getattr(client, "uploads", 0), 0)
        # featured_media untouched: the apply must not set a generic featured.
        self.assertNotIn("featured_media", client.updated[0][1] if client.updated else {})

    def test_apply_reuses_media_library_attachment_as_new_upload(self):
        payload = editorial_payload()
        payload["cleaned_html"] = (
            "<p>Um.</p><p>Dois.</p><p>Tres.</p><p>Quatro.</p><p>Cinco.</p>"
            '<figure><img src="https://s3.example/noticia-importante-1.webp" alt="Título sobre videogame e lançamento importante" width="800" height="450" />'
            "<figcaption>Crédito: Autor. Licença CC BY 4.0 (https://creativecommons.org/licenses/by/4.0).</figcaption></figure>"
            '<figure><img src="https://s3.example/noticia-importante-2.webp" alt="Título sobre videogame e lançamento importante" width="800" height="450" />'
            "<figcaption>Crédito: Autor. Licença CC BY 4.0 (https://creativecommons.org/licenses/by/4.0).</figcaption></figure>"
        )
        # Reuse item: media_library_id references an existing attachment whose
        # title carries the credit block. The apply must download from the
        # attachment URL and upload a NEW attachment (original untouched).
        item = self.media_item(paragraph_index=1, is_featured=True)
        item["media_library_id"] = 500
        payload["media_plan"] = [item]
        attachment = {
            "id": 500,
            "source_url": "https://wp.test/uploads/reuse-source.webp",
            "media_details": {"width": 1920, "height": 1080},
            "alt_text": "Titulo sobre videogame",
            "title": {"rendered": "Crédito da imagem: Autor Original. Titulo sobre videogame. CC BY 4.0."},
            "caption": {"rendered": ""},
        }

        class ReuseClient(FakeClient):
            def get_media(self, media_id):
                if media_id == 500:
                    return attachment
                return super().get_media(media_id)

        with mock.patch("unicornio_editor.workflow.download_image", return_value=Path("/tmp/reuse.webp")) as dl, mock.patch(
            "unicornio_editor.workflow.prepare_featured_webp", return_value=Path("/tmp/reuse_featured.webp")
        ), mock.patch(
            "unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "teste")
        ), mock.patch(
            "unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)
        ), mock.patch(
            "unicornio_editor.workflow.upload_image",
            return_value={"id": 77, "source_url": "https://wp.test/uploads/reuse-featured-1280x720.webp"},
        ):
            with tempfile.TemporaryDirectory() as directory:
                client = ReuseClient(self.post())
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        dl.assert_called_once_with(
            "https://wp.test/uploads/reuse-source.webp", mock.ANY, max_attempts=3
        )
        self.assertEqual(report["featured_media"], 77)
        self.assertEqual(client.updated[0][1]["featured_media"], 77)

    def test_apply_rejects_media_library_reuse_without_credit(self):
        payload = editorial_payload()
        payload["cleaned_html"] = (
            "<p>Um.</p><p>Dois.</p><p>Tres.</p><p>Quatro.</p><p>Cinco.</p>"
            '<figure><img src="https://s3.example/noticia-importante-1.webp" alt="Título sobre videogame e lançamento importante" width="800" height="450" />'
            "<figcaption>Crédito: Autor. Licença CC BY 4.0 (https://creativecommons.org/licenses/by/4.0).</figcaption></figure>"
            '<figure><img src="https://s3.example/noticia-importante-2.webp" alt="Título sobre videogame e lançamento importante" width="800" height="450" />'
            "<figcaption>Crédito: Autor. Licença CC BY 4.0 (https://creativecommons.org/licenses/by/4.0).</figcaption></figure>"
        )
        item = self.media_item(paragraph_index=1, is_featured=True)
        item["media_library_id"] = 501
        payload["media_plan"] = [item]
        attachment = {
            "id": 501,
            "source_url": "https://wp.test/uploads/no-credit.jpg",
            "media_details": {"width": 1920, "height": 1080},
            "alt_text": "",
            "title": {"rendered": "Foto sem credito"},
            "caption": {"rendered": ""},
        }

        class NoCreditClient(FakeClient):
            def get_media(self, media_id):
                if media_id == 501:
                    return attachment
                return super().get_media(media_id)

        with mock.patch("unicornio_editor.workflow.download_image") as dl:
            with tempfile.TemporaryDirectory() as directory:
                client = NoCreditClient(self.post())
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        dl.assert_not_called()
        self.assertEqual(report["media_plan_results"][0]["status"], "rejected")

    def test_apply_keeps_compliant_featured_image(self):
        post = self.post()
        post["featured_media"] = 7
        # Already 1280x720 WebP AND carrying a cited-work evidence -> kept.
        media = {
            "id": 7,
            "source_url": "https://wp.test/uploads/noticia-sobre-videogame-e-lancamento-importante-1280x720.webp",
            "media_details": {"width": 1280, "height": 720},
            "alt_text": "Notícia sobre videogame e lançamento importante",
            "title": {"rendered": "Notícia sobre videogame e lançamento importante"},
        }

        class MediaClient(FakeClient):
            def get_media(self, media_id):
                return media

        with mock.patch("unicornio_editor.workflow.upload_image") as upload:
            with tempfile.TemporaryDirectory() as directory:
                client = MediaClient(post)
                report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload())
        upload.assert_not_called()
        self.assertEqual(client.updated[0][1]["featured_media"], 7)
        self.assertEqual(report["featured_media"], 7)

    def test_apply_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(True), Path(directory), 42, editorial_payload())
            self.assertFalse(report["wordpress_changed"])
            self.assertEqual(report["dry_run"], True)
            self.assertEqual(client.updated, [])

    def test_apply_saves_editorial_latest_for_publish_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload())
            saved = Path(directory) / "backups" / "42" / "editorial.latest.json"
            self.assertTrue(saved.is_file())
            self.assertIn("cleaned_html", json.loads(saved.read_text(encoding="utf-8")))

    def test_publish_skips_without_editorial_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            report = publish_post(client, self.config(False), Path(directory), 42)
        self.assertFalse(report["wordpress_changed"])
        self.assertEqual(report["status"], "skipped")
        self.assertIn("editorial.latest.json", report["reason"])
        self.assertEqual(client.updated, [])

    def test_publish_blocks_when_checklist_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backups" / "42").mkdir(parents=True)
            (root / "backups" / "42" / "editorial.latest.json").write_text(
                json.dumps(editorial_payload()), encoding="utf-8"
            )
            client = FakeClient(self.post())
            report = publish_post(client, self.config(False), root, 42)
        self.assertFalse(report["wordpress_changed"])
        self.assertEqual(report["status"], "blocked")
        self.assertGreater(report["checklist"]["failed"], 0)
        self.assertEqual(client.updated, [])

    def checklist_pass_post(self):
        post = self.post()
        post["content"] = {
            "raw": (
                "<p>Texto revisado sobre videogame.</p>"
                '<figure class="aligncenter"><img src="https://wp.test/1.webp" width="1280" height="720" alt="Imagem do titulo do jogo" />'
                "<figcaption>Crédito da imagem: Autor. Imagem do titulo do jogo. CC BY 4.0.</figcaption></figure>"
                "<p>Mais texto sobre videogame e jogos.</p>"
                '<figure class="aligncenter"><img src="https://wp.test/2.webp" width="1280" height="720" alt="Imagem do titulo do jogo" />'
                "<figcaption>Crédito da imagem: Autor. Imagem do titulo do jogo. CC BY 4.0.</figcaption></figure>"
                '<p>Fonte: <a href="https://source.example/news" rel="nofollow noopener">Source</a>.</p>'
                "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
            )
        }
        post["featured_media"] = 7
        return post

    def test_featured_filename_from_source_keeps_provenance(self):
        from unicornio_editor.workflow import _featured_filename_from_source

        self.assertEqual(
            _featured_filename_from_source(
                "https://s3.example/uploads/2026/08/Remothered-Red-Nuns-Legacy-Launches.jpg"
            ),
            "remothered-red-nuns-legacy-launches-1280x720.webp",
        )
        # Generic/short or non-ascii stems fall back to the generic name.
        self.assertEqual(
            _featured_filename_from_source("https://s3.example/uploads/2026/08/x.jpg"),
            "featured-1280x720.webp",
        )
        self.assertEqual(
            _featured_filename_from_source("https://s3.example/foto-çã.jpg"),
            "featured-1280x720.webp",
        )

    def test_publish_blocks_without_publish_enabled_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backups" / "42").mkdir(parents=True)
            (root / "backups" / "42" / "editorial.latest.json").write_text(
                json.dumps(editorial_payload()), encoding="utf-8"
            )
            client = FakeClient(self.checklist_pass_post())
            config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
            report = publish_post(client, config, root, 42)
        self.assertFalse(report["wordpress_changed"])
        self.assertEqual(report["status"], "blocked")
        self.assertIn("PUBLISH_ENABLED", report["reason"])
        self.assertEqual(client.updated, [])

    def test_publish_publishes_when_all_gates_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backups" / "42").mkdir(parents=True)
            (root / "backups" / "42" / "editorial.latest.json").write_text(
                json.dumps(editorial_payload()), encoding="utf-8"
            )
            client = FakeClient(self.checklist_pass_post())
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False, publish_enabled=True
            )
            report = publish_post(client, config, root, 42)
        self.assertTrue(report["wordpress_changed"])
        self.assertEqual(report["status"], "published")
        self.assertEqual(report["status_after"], "publish")
        self.assertEqual(client.updated[0][0], 42)
        self.assertEqual(client.updated[0][1]["status"], "publish")
        self.assertIn("_ai_editor_published_at", client.updated[0][1]["meta"])

    def test_publish_ready_respects_window_limit(self):
        class QueueClient(FakeClient):
            def __init__(self, posts):
                super().__init__(posts[0])
                self.posts = posts

            def get_post(self, post_id):
                return next(p for p in self.posts if p["id"] == post_id)

            def list_pending(self, per_page=50):
                return self.posts

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for post_id in (1, 2, 3):
                (root / "backups" / str(post_id)).mkdir(parents=True)
                (root / "backups" / str(post_id) / "editorial.latest.json").write_text(
                    json.dumps(editorial_payload()), encoding="utf-8"
                )
            queue = [
                {**self.checklist_pass_post(), "id": 1},
                {**self.checklist_pass_post(), "id": 2},
                {**self.checklist_pass_post(), "id": 3},
            ]
            client = QueueClient(queue)
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True, publish_limit=2,
            )
            outcomes = publish_ready_posts(client, config, root, limit=config.publish_limit)
        published = [o for o in outcomes if o.get("wordpress_changed")]
        self.assertEqual(len(published), 2)
        self.assertEqual([o["post_id"] for o in published], [1, 2])
        self.assertEqual(len(client.updated), 2)


    def test_apply_fails_fast_without_minimum_images(self):
        # Politica 2/4/6 sem waiver: um editorial cujo conteudo nao atinge o
        # minimo de imagens NAO pode ser gravado — o apply recusa (needs_rework),
        # arquiva editorial.blocked.json e devolve o post a fila.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = editorial_payload()
            payload["cleaned_html"] = "<p>Texto revisado sobre videogame.</p>"  # 0 imagens
            payload["media_plan"] = []
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), root, 42, payload)
            self.assertEqual(report["status"], "needs_rework")
            self.assertIn("imagens_no_corpo", report["blocked_reasons"])
            self.assertFalse(report["wordpress_changed"])
            self.assertEqual(client.updated, [])
            self.assertFalse((root / "backups/42/editorial.latest.json").exists())
            self.assertTrue((root / "backups/42/editorial.blocked.json").is_file())

    def test_publish_blocked_records_rework_but_keeps_latest(self):
        # Gate duplo fechado: o publish bloqueia o post e registra
        # editorial.blocked.json, MAS mantém editorial.latest.json — o post
        # continua candidato nas próximas janelas (remover o latest orfanaria
        # o post mesmo com conteúdo bom já gravado no WordPress).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backups" / "42").mkdir(parents=True)
            payload = editorial_payload()
            payload["cleaned_html"] = "<p>Texto revisado sobre videogame.</p>"  # falha no checklist
            (root / "backups" / "42" / "editorial.latest.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            client = FakeClient(self.post())
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True,
            )
            report = publish_post(client, config, root, 42)
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(report["reopened_for_rework"])
            self.assertFalse(report["wordpress_changed"])
            self.assertTrue((root / "backups/42/editorial.latest.json").is_file())
            self.assertTrue((root / "backups/42/editorial.blocked.json").is_file())


if __name__ == "__main__":
    unittest.main()
