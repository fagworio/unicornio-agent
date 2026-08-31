import datetime
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
    discard_post,
    get_cleaned_content,
    load_draft,
    prepare_post,
    publish_post,
    publish_ready_posts,
    retry_post,
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
        # Espelha o WordPress: o update muta o post (content/meta/featured).
        if isinstance(payload.get("content"), dict):
            self.post.setdefault("content", {})["raw"] = payload["content"]["raw"]
        if isinstance(payload.get("meta"), dict):
            self.post.setdefault("meta", {}).update(payload["meta"])
        if "featured_media" in payload:
            self.post["featured_media"] = payload["featured_media"]
        return {"id": post_id, "status": "pending", **payload}

    def publish(self, post_id, meta=None, date_gmt=None):
        payload = {"status": "publish"}
        if date_gmt:
            payload["date_gmt"] = date_gmt
        if meta:
            payload["meta"] = meta
        self.updated.append((post_id, payload))
        return {
            "id": post_id,
            "status": "publish",
            "link": f"https://wp.test/?p={post_id}",
            **payload,
        }

    def move_to_status(self, post_id, status):
        self.updated.append((post_id, {"status": status}))
        return {"id": post_id, "status": status}


class WorkflowTests(unittest.TestCase):
    def config(self, dry_run):
        return Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=dry_run)

    def post(self):
        # Data dinamica (now - 2d): o build_queue_report so lista como
        # "recent" posts com < 7 dias; data fixa tornava o teste sensivel ao
        # tempo (quebrava quando o fixture envelhecia).
        recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
        stamp = recent.strftime("%Y-%m-%dT%H:%M:%S")
        return {
            "id": 42,
            "status": "pending",
            "date": stamp,
            "date_gmt": stamp,
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

    def test_apply_refuses_a_post_held_by_another_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            from unicornio_editor.locking import LockManager

            lock = LockManager(root / "work" / "locks", ttl=900).acquire(42)
            try:
                with self.assertRaisesRegex(Exception, "already being processed"):
                    apply_editorial(client, self.config(False), root, 42, editorial_payload())
            finally:
                lock.release()

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

    def test_queue_paginates_beyond_the_first_page(self):
        class PagedClient(FakeClient):
            def __init__(self, posts):
                super().__init__(posts[0])
                self.posts = posts

            def list_pending(self, *, page=1, per_page=50, status="pending", **_kwargs):
                if status != "pending":
                    return []
                start = (page - 1) * per_page
                return self.posts[start:start + per_page]

        with tempfile.TemporaryDirectory() as directory:
            posts = []
            for post_id in range(1, 53):
                post = self.post()
                post["id"] = post_id
                posts.append(post)
            report = build_queue_report(PagedClient(posts), Path(directory), per_page=50)
        self.assertEqual(report["pending"], 52)
        self.assertEqual(report["unprocessed_ids"][-1], 52)

    def test_cards_finds_eligible_post_after_first_page(self):
        class PagedClient(FakeClient):
            def __init__(self, posts):
                super().__init__(posts[0])
                self.posts = posts

            def list_pending(self, *, page=1, per_page=20, include=None, **_kwargs):
                if include:
                    return [post for post in self.posts if post["id"] in include]
                start = (page - 1) * per_page
                return self.posts[start:start + per_page]

        with tempfile.TemporaryDirectory() as directory:
            posts = []
            for post_id in range(1, 22):
                post = self.post()
                post["id"] = post_id
                post["meta"]["_hermes_state"] = "ready" if post_id <= 20 else "new"
                posts.append(post)
            cards = build_cards(PagedClient(posts), self.config(False), Path(directory), per_page=2)
        self.assertEqual([card["id"] for card in cards["cards"]], [21])

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
            # uncertain vence para blocked e exige retry humano; nunca volta
            # sozinho para evitar rework infinito.
            (root / "backups" / "42" / "uncertain.json").write_text(
                json.dumps({"status": "uncertain"}), encoding="utf-8"
            )
            report = build_queue_report(client, root)
            self.assertEqual(report["blocked"], 0)
            self.assertEqual(report["uncertain"], 1)
            self.assertEqual(report["blocked_ids"], [])
            self.assertEqual(report["eligible_rework_ids"], [])

    def test_queue_monitor_excludes_old_backlog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = self.post()
            old_stamp = (
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=90)
            ).strftime("%Y-%m-%dT%H:%M:%S")
            old["date"] = old_stamp
            old["date_gmt"] = old_stamp
            report = build_queue_report(FakeClient(old), root)
            self.assertEqual(report["unprocessed_ids"], [42])
            self.assertEqual(report["recent_unprocessed_ids"], [])

    def test_queue_lists_wp_awaiting_human_status_posts(self):
        # Posts movidos para o status WP "awaiting_human" (decisao humana via
        # filtro na tela de posts) saem de pending mas DEVEM continuar
        # aparecendo no relatorio como awaiting_human — e nunca como trabalho
        # do monitor (nao entram em unprocessed/recent_unprocessed).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            post = self.post()

            class WpStatusClient(FakeClient):
                def list_pending(self, **kwargs):
                    if kwargs.get("status") == "awaiting_human":
                        ah = dict(post)
                        ah["id"] = 99
                        ah["status"] = "awaiting_human"
                        ah["_wp_awaiting_human"] = True
                        return [ah]
                    return [post]

            report = build_queue_report(WpStatusClient(post), root)
            self.assertEqual(report["awaiting_human_ids"], [99])
            self.assertEqual(report["unprocessed_ids"], [42])
            self.assertEqual(report["recent_unprocessed_ids"], [42])

    def test_queue_uncertain_in_cooldown_stays_out_of_rework(self):
        # Uncertain com next_retry_at no FUTURO nao volta ao trabalho (evita
        # loop infinito de re-trilhagem queimando tokens); com cooldown
        # expirado/vazio volta ao eligible_rework.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            future = (
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
            ).isoformat(timespec="seconds")
            post = self.post()
            post["meta"] = {
                "_hermes_state": "uncertain",
                "_hermes_next_retry_at": future,
            }
            (root / "backups" / "42").mkdir(parents=True)
            (root / "backups" / "42" / "uncertain.json").write_text(
                json.dumps({"status": "uncertain"}), encoding="utf-8"
            )
            report = build_queue_report(FakeClient(post), root)
            self.assertEqual(report["uncertain"], 1)
            self.assertEqual(report["eligible_rework_ids"], [])

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
            self.assertTrue(report["wordpress_changed"])
            self.assertTrue(report["baseline_enriched"])
            self.assertIn("Confira mais novidades", client.post["content"]["raw"])
            self.assertIn("Fonte:", client.post["content"]["raw"])
            self.assertTrue(Path(directory, "backups/42/uncertain.json").is_file())
            self.assertFalse(Path(directory, "backups/42/editorial.latest.json").exists())

    def test_apply_confident_skip_is_final(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = editorial_payload("skip")
            payload["site_relevance"]["confidence"] = 0.99
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
            self.assertTrue(report["wordpress_changed"])
            self.assertTrue(report["baseline_enriched"])
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
            self.assertTrue(card["featured"]["exists"])
            # Delta de imagens: 1 relevante de 2 exigidas (minimo 2/4/6).
            self.assertEqual(card["images"]["required"], 2)
            self.assertEqual(card["images"]["valid"], 1)
            self.assertEqual(card["images"]["missing"], 1)
            self.assertEqual(card["images"]["irrelevant"], 0)
            self.assertEqual(card["images"]["non_webp"], 0)
            # Featured existente (hardcoded do fake) nao retrata Redfall.
            self.assertEqual(card["featured"]["action"], "replace")
            self.assertTrue(card["seo_exists"])
            self.assertTrue(card["game_hint"])
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
            self.assertEqual(cards[0]["state"], "blocked")  # latest existe, mas blocked
            self.assertEqual(
                cards[0]["blocked_reason"],
                "checklist: imagens_no_corpo, destaque_1280x720",
            )
            # Plano de correcao derivado do checklist bloqueado (Fase 4.3):
            # o agente sabe o que corrigir SÓ pelo card.
            self.assertEqual(cards[0]["fix"]["find_inline_images"], 2)
            self.assertTrue(cards[0]["fix"]["replace_featured"])
            self.assertFalse(cards[0]["fix"]["fix_list_structure"])
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
            # Fill amplo (a maioria dos pending pode ser ready/out-of-queue);
            # o custo de tokens e so dos cards impressos, nao do fetch.
            self.assertEqual(client.calls[1]["per_page"], 20)

    def test_build_cards_includes_eligible_uncertain_and_skips_in_cooldown(self):
        # Espelha a politica do dono (ba91a43): uncertain com cooldown
        # expirado (ou legado sem cooldown) volta ao trabalho — o card DEVE
        # aparecer, senao o monitor acorda o agente para posts que o cards
        # nunca mostra (loop de tokens). Uncertain em cooldown continua fora.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            future = (
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
            ).isoformat(timespec="seconds")
            eligible = self.post()  # id 42, legado: sem meta de estado
            eligible["title"] = {"raw": "Uncertain legado volta ao trabalho"}
            in_cooldown = dict(eligible)
            in_cooldown["id"] = 43
            in_cooldown["title"] = {"raw": "Uncertain em cooldown fica fora"}
            in_cooldown["meta"] = {
                "_hermes_state": "uncertain",
                "_hermes_next_retry_at": future,
            }
            (root / "backups" / "42").mkdir(parents=True)
            (root / "backups" / "42" / "uncertain.json").write_text(
                json.dumps({"status": "uncertain"}), encoding="utf-8"
            )
            (root / "backups" / "43").mkdir(parents=True)
            (root / "backups" / "43" / "uncertain.json").write_text(
                json.dumps({"status": "uncertain"}), encoding="utf-8"
            )

            class FakeTwo(FakeClient):
                def list_pending(self, **kwargs):
                    include = kwargs.get("include")
                    candidates = [eligible, in_cooldown]
                    if include:
                        candidates = [p for p in candidates if p["id"] in include]
                    return candidates[: kwargs.get("per_page", 10)]

            report = build_cards(FakeTwo(eligible), self.config(True), root)
            ids = [c["id"] for c in report["cards"]]
            self.assertEqual(ids, [])

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

    def test_apply_skip_persists_safe_baseline_but_not_editorial_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            post = self.post()
            post["content"] = {"raw": "<h1>Titulo duplicado</h1><p>Notícia sobre Xbox.</p>"}
            client = FakeClient(post)
            report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload("skip"))
            self.assertTrue(report["wordpress_changed"])
            self.assertEqual(report["status"], "skipped")
            self.assertEqual(len(client.updated), 2)
            content = client.updated[0][1]["content"]["raw"]
            self.assertNotIn("<h1>", content)
            self.assertIn("Confira mais novidades", content)
            self.assertIn("Fonte:", content)
            self.assertIn('href="https://www.unicorniohater.com.br/games/x-box/"', content)
            self.assertNotIn("Texto revisado", content)
            self.assertEqual(client.updated[1][1]["meta"]["_hermes_state"], "skipped")

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
        # O texto precisa conter a keyword (qualidade_texto faz parte do
        # preflight completo desde a Fase 2 — o apply bloqueia qualquer falha).
        payload["cleaned_html"] = (
            "<p>Um videogame.</p><p>Dois.</p><p>Tres.</p><p>Quatro.</p>"
            "<p>Cinco.</p><p>Seis.</p><p>Sete.</p>"
        )
        payload["media_plan"] = [
            {**self.media_item(paragraph_index=0), "direct_image_url": "https://source.example/a.jpg"},
            {**self.media_item(paragraph_index=3), "direct_image_url": "https://source.example/b.jpg"},
            {
                **self.media_item(paragraph_index=6, is_featured=True),
                "direct_image_url": "https://source.example/keyart.jpg",
            },
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
            side_effect=lambda client, webp, evidence: {
                "a.jpg": {"id": 50, "source_url": "https://wp.test/50.webp"},
                "b.jpg": {"id": 51, "source_url": "https://wp.test/51.webp"},
                "keyart.jpg": {"id": 52, "source_url": "https://wp.test/52.webp"},
            }[evidence["direct_image_url"].split("/")[-1]],
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

    def test_media_plan_falls_back_to_serial_when_pool_cannot_start(self):
        # Se o executor nem iniciar, não houve efeito colateral e é seguro
        # processar o lote em série.
        payload = editorial_payload()
        payload["cleaned_html"] = (
            "<p>Um videogame.</p><p>Dois.</p><p>Tres.</p><p>Quatro.</p>"
            "<p>Cinco.</p><p>Seis.</p><p>Sete.</p>"
        )
        payload["media_plan"] = [
            {**self.media_item(paragraph_index=0), "direct_image_url": "https://source.example/a.jpg"},
            {**self.media_item(paragraph_index=3), "direct_image_url": "https://source.example/b.jpg"},
            {
                **self.media_item(paragraph_index=6, is_featured=True),
                "direct_image_url": "https://source.example/keyart.jpg",
            },
        ]
        with mock.patch(
            "unicornio_editor.workflow.download_image", return_value=Path("/tmp/source.jpg")
        ), mock.patch(
            "unicornio_editor.workflow.convert_to_webp", return_value=Path("/tmp/inline.webp")
        ), mock.patch(
            "unicornio_editor.workflow.prepare_featured_webp", return_value=Path("/tmp/featured.webp")
        ), mock.patch(
            "unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "teste")
        ), mock.patch(
            "unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)
        ), mock.patch(
            "unicornio_editor.workflow.upload_image",
            side_effect=lambda client, webp, evidence: {
                "a.jpg": {"id": 50, "source_url": "https://wp.test/50.webp"},
                "b.jpg": {"id": 51, "source_url": "https://wp.test/51.webp"},
                "keyart.jpg": {"id": 52, "source_url": "https://wp.test/52.webp"},
            }[evidence["direct_image_url"].split("/")[-1]],
        ), mock.patch(
            "unicornio_editor.workflow.ThreadPoolExecutor",
            side_effect=RuntimeError("pool indisponivel"),
        ):
            with tempfile.TemporaryDirectory() as directory:
                client = FakeClient(self.post())
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        self.assertEqual(client.updated[0][1]["featured_media"], 52)
        self.assertEqual(report["featured_media"], 52)
        self.assertTrue(report["media_plan_results"][2]["featured"])

    def test_media_worker_error_does_not_repeat_successful_uploads(self):
        payload = editorial_payload()
        payload["cleaned_html"] = "<p>Um videogame.</p><p>Dois.</p><p>Tres.</p><p>Quatro.</p>"
        payload["media_plan"] = [
            {**self.media_item(paragraph_index=0), "direct_image_url": "https://source.example/a.jpg"},
            {**self.media_item(paragraph_index=2, is_featured=True), "direct_image_url": "https://source.example/keyart.jpg"},
        ]
        uploads: list[str] = []

        def upload_once(_client, _webp, evidence):
            name = evidence["direct_image_url"].rsplit("/", 1)[-1]
            uploads.append(name)
            if name == "keyart.jpg":
                raise RuntimeError("upload indisponivel")
            return {"id": 50, "source_url": "https://wp.test/50.webp"}

        with mock.patch("unicornio_editor.workflow.download_image", return_value=Path("/tmp/source.jpg")), mock.patch(
            "unicornio_editor.workflow.convert_to_webp", return_value=Path("/tmp/inline.webp")
        ), mock.patch(
            "unicornio_editor.workflow.prepare_featured_webp", return_value=Path("/tmp/featured.webp")
        ), mock.patch(
            "unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "teste")
        ), mock.patch(
            "unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)
        ), mock.patch("unicornio_editor.workflow.upload_image", side_effect=upload_once):
            with tempfile.TemporaryDirectory() as directory:
                report = apply_editorial(FakeClient(self.post()), self.config(False), Path(directory), 42, payload)

        self.assertEqual(sorted(uploads), ["a.jpg", "keyart.jpg"])
        failed = [item for item in report["media_plan_results"] if item.get("status") == "error"]
        self.assertEqual(len(failed), 1)

    def test_media_exhausted_listicle_goes_awaiting_human(self):
        # Listicle que esgotou a busca de imagens NAO fica em loop de rework:
        # vai DIRETO para AWAITING_HUMAN (revisao manual), sem queimar token.
        payload = editorial_payload()
        payload["media_exhausted"] = True
        payload["seo"] = {
            "title": "10 melhores jogos",
            "meta_description": "Uma descrição suficientemente longa sobre o conteúdo de videogame, seus detalhes, plataformas e contexto para o leitor entender a notícia.",
            "focus_keyword": "videogame",
        }
        payload["cleaned_html"] = (
            "<h2>1. Jogo: titulo</h2><p>Descricao do primeiro jogo.</p>"
            "<h2>2. Jogo: titulo</h2><p>Descricao do segundo jogo.</p>"
        )
        payload["media_plan"] = [
            {
                **self.media_item(paragraph_index=0, is_featured=True),
                "direct_image_url": "https://source.example/keyart.jpg",
            },
        ]
        with mock.patch(
            "unicornio_editor.workflow.download_image", return_value=Path("/tmp/source.jpg")
        ), mock.patch(
            "unicornio_editor.workflow.prepare_featured_webp", return_value=Path("/tmp/featured.webp")
        ), mock.patch(
            "unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "teste")
        ), mock.patch(
            "unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)
        ), mock.patch(
            "unicornio_editor.workflow.upload_image",
            return_value={"id": 52, "source_url": "https://wp.test/52.webp"},
        ):
            with tempfile.TemporaryDirectory() as directory:
                client = FakeClient(self.post())
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        self.assertEqual(report["state"], "awaiting_human")
        self.assertEqual(report["status"], "needs_rework")

    def test_article_deterministic_waiver_on_second_apply(self):
        # SEM media_exhausted do LLM: um ARTIGO falhando em imagens + featured
        # waiva DETERMINISTICAMENTE no 2o apply (teto max_media_search_attempts=2).
        payload = editorial_payload()
        payload["cleaned_html"] = "<p>Texto revisado sobre videogame.</p>"  # 0 imagens
        payload["media_plan"] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            first = apply_editorial(client, self.config(False), root, 42, payload)
            second = apply_editorial(client, self.config(False), root, 42, payload)
        self.assertEqual(first["state"], "blocked")
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(second["state"], "ready")

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
                if media_id == 88:
                    # Apos o re-upload, o WP retorna o NOVO attachment (WebP
                    # 1280x720 com a proveniencia preservada no title/alt).
                    return {
                        "id": 88,
                        "source_url": "https://wp.test/uploads/noticia-sobre-videogame-e-lancamento-importante-1280x720.webp",
                        "media_details": {"width": 1280, "height": 720},
                        "alt_text": "Notícia sobre videogame e lançamento importante",
                        "title": {"rendered": "Notícia sobre videogame e lançamento importante"},
                    }
                return media

            def upload_media(self, path, *, filename, alt_text, title, caption=None):
                self.uploads = getattr(self, "uploads", 0) + 1
                return {"id": 88, "source_url": "https://wp.test/uploads/noticia-sobre-videogame-e-lancamento-importante-1280x720.webp"}

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
            "<p>Um videogame.</p><p>Dois.</p><p>Tres.</p><p>Quatro.</p><p>Cinco.</p>"
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
                "https://wp.test/uploads/reuse-source.webp", mock.ANY,
                max_attempts=3, url_policy="audit", audit=mock.ANY,
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
            # O publish reabriu para rework: estado blocked gravado no WP.
            self.assertEqual(len(client.updated), 1)
            self.assertEqual(client.updated[0][1]["meta"]["_hermes_state"], "blocked")
            self.assertTrue((root / "backups/42/editorial.blocked.json").is_file())

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

            def list_pending(self, per_page=50, page=1):
                return self.posts if page <= 1 else []

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
        # minimo de imagens NAO pode ser gravado — o apply recusa (needs_rework)
        # e arquiva editorial.blocked.json. O editorial.latest.json permanece
        # (o post mantém a candidatura; o publish decide com o conteudo real).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = editorial_payload()
            payload["cleaned_html"] = "<p>Texto revisado sobre videogame.</p>"  # 0 imagens
            payload["media_plan"] = []
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), root, 42, payload)
            self.assertEqual(report["status"], "needs_rework")
            self.assertIn("imagens_no_corpo", report["blocked_reasons"])
            self.assertTrue(report["wordpress_changed"])
            self.assertTrue(report.get("baseline_enriched"))
            # Baseline de conteudo (CTA/Fonte/links) + telemetria de estado.
            self.assertEqual(len(client.updated), 2)
            state_meta = client.updated[1][1]["meta"]
            self.assertEqual(state_meta["_hermes_state"], "blocked")
            self.assertEqual(state_meta["_hermes_attempts"], "1")
            self.assertNotEqual(state_meta["_hermes_next_retry_at"], "")
            self.assertEqual(report["state"], "blocked")
            self.assertEqual(report["attempts"], 1)
            self.assertTrue((root / "backups/42/editorial.latest.json").is_file())
            self.assertTrue((root / "backups/42/editorial.blocked.json").is_file())
            # Draft preservado para o rework incremental (Fase 3).
            self.assertTrue((root / "backups/42/editorial.draft.json").is_file())

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

    def test_normalize_inline_images_converts_relevant_non_webp(self):
        # Fase 5.2: imagem inline relevante fora do formato (JPEG) vira WebP
        # local automaticamente — problema tecnico nao volta ao modelo.
        # Irrelevante fica como esta (gate relevancia bloqueia e o agente decide).
        from unicornio_editor.media.relevance import extract_entities
        from unicornio_editor.workflow import _normalize_inline_images

        html = (
            '<figure><img src="https://s3.example/redfall-key-art.webp" width="800" height="450" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            '<figure><img src="https://s3.example/redfall-screenshot.jpg" alt="Redfall screenshot" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            '<figure><img src="https://s3.example/gatinho.jpg" alt="gatinho fofo" />'
            "<figcaption>Crédito da imagem: Autor. CC0.</figcaption></figure>"
        )
        entities = extract_entities(title="Redfall ganha novo gameplay", content_html=html)

        class UploadClient:
            def __init__(self):
                self.uploads = []

            def upload_media(self, path, *, filename, alt_text, title, caption=None):
                self.uploads.append({"filename": filename, "alt_text": alt_text})
                return {"id": 99, "source_url": "https://wp.test/uploads/redfall-screenshot-800x450.webp"}

        client = UploadClient()
        with mock.patch("unicornio_editor.workflow.download_image", return_value=Path("/tmp/s.jpg")), mock.patch(
            "unicornio_editor.workflow.convert_to_webp", return_value=Path("/tmp/inline.webp")
        ), mock.patch("unicornio_editor.workflow.image_dimensions", return_value=(800, 450)):
            normalized, results = _normalize_inline_images(client, self.config(False), html, entities)

        self.assertIn("https://wp.test/uploads/redfall-screenshot-800x450.webp", normalized)
        self.assertIn("width=\"800\" height=\"450\"", normalized)
        # WebP original intocado; irrelevante intocada.
        self.assertIn("redfall-key-art.webp", normalized)
        self.assertIn("gatinho.jpg", normalized)
        self.assertEqual(len(client.uploads), 1)
        self.assertIn("redfall-screenshot", client.uploads[0]["filename"])
        statuses = {r["status"] for r in results}
        self.assertIn("normalized", statuses)
        self.assertIn("irrelevant", statuses)

    def test_apply_saves_draft_before_heavy_execution(self):
        # Fase 3: o rascunho editorial e persistido ANTES da execucao pesada —
        # mesmo com falha de midia/checklist, o trabalho editorial fica salvo.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = editorial_payload()
            payload["cleaned_html"] = "<p>Texto revisado sobre videogame.</p>"
            payload["media_plan"] = []
            client = FakeClient(self.post())
            apply_editorial(client, self.config(False), root, 42, payload)  # falha (0 imagens)
            draft = json.loads(
                (root / "backups/42/editorial.draft.json").read_text(encoding="utf-8")
            )
            self.assertEqual(draft["seo"]["focus_keyword"], "videogame")
            self.assertIn("cleaned_html", draft)
            # load_draft devolve o rascunho (base do rework incremental).
            loaded = load_draft(root, 42)
            self.assertEqual(loaded["seo"]["title"], draft["seo"]["title"])

    def test_apply_full_checklist_gate_blocks_any_failure(self):
        # Fase 2: QUALQUER falha do checklist impede READY — nao so
        # imagens_no_corpo. Post sem featured e com media_plan vazio falha
        # imagem_destaque e nunca e gravado.
        post = self.post()
        post["featured_media"] = 0
        payload = editorial_payload()
        payload["cleaned_html"] = (
            "<p>Texto revisado sobre videogame e lancamento.</p>"
            '<figure><img src="https://s3.example/a.webp" alt="Título sobre videogame e lançamento importante" width="800" height="450" />'
            "<figcaption>Crédito: Autor. Licença CC BY 4.0 (https://creativecommons.org/licenses/by/4.0).</figcaption></figure>"
            '<figure><img src="https://s3.example/b.webp" alt="Título sobre videogame e lançamento importante" width="800" height="450" />'
            "<figcaption>Crédito: Autor. Licença CC BY 4.0 (https://creativecommons.org/licenses/by/4.0).</figcaption></figure>"
        )
        payload["media_plan"] = []
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(post)
            report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
            self.assertEqual(report["status"], "needs_rework")
            self.assertIn("imagem_destaque", report["blocked_reasons"])
            self.assertNotIn("imagens_no_corpo", report["blocked_reasons"])
            self.assertEqual(client.updated[1][1]["meta"]["_hermes_state"], "blocked")
            # Baseline de conteudo (CTA/Fonte/links) e gravado; o editorial
            # incompleto (texto/midia) fica apenas no draft.
            self.assertIn("content", client.updated[0][1])

    def test_rework_backoff_escalates_to_awaiting_human(self):
        # Fase 8: 1a falha +30m (blocked), 2a +2h (blocked), 3a AWAITING_HUMAN.
        # Usa falha NAO de imagem (keyword ausente) porque o teto deterministico
        # (max_media_search_attempts=2) resolve falha de imagem + featured no 2o
        # apply (artigo waiva -> ready; listicle -> awaiting_human).
        payload = editorial_payload()
        payload["seo"]["focus_keyword"] = "xbox"  # ausente no corpo -> qualidade_texto (nao-imagem)
        payload["cleaned_html"] = "<p>Texto revisado sobre videogame.</p>"
        payload["media_plan"] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            first = apply_editorial(client, self.config(False), root, 42, payload)
            second = apply_editorial(client, self.config(False), root, 42, payload)
            third = apply_editorial(client, self.config(False), root, 42, payload)
            self.assertEqual(first["state"], "blocked")
            self.assertEqual(first["attempts"], 1)
            self.assertNotEqual(first["next_retry_at"], "")
            self.assertEqual(second["state"], "blocked")
            self.assertEqual(second["attempts"], 2)
            self.assertEqual(third["state"], "awaiting_human")
            self.assertEqual(third["attempts"], 3)
            self.assertEqual(third["next_retry_at"], "")
            # AWAITING_HUMAN sai da fila de trabalho do monitor.
            queue = build_queue_report(client, root)
            self.assertEqual(queue["awaiting_human"], 1)
            self.assertEqual(queue["eligible_rework_ids"], [])
            self.assertIn(42, queue["awaiting_human_ids"])
            # Fluxo novo (dono): ao esgotar, o pipeline MOVE o status WP para
            # awaiting_human — o post aparece no filtro do WordPress.
            status_moves = [m for m in client.updated if "status" in m[1]]
            self.assertEqual(status_moves, [(42, {"status": "awaiting_human"})])

    def test_apply_success_marks_ready_with_manifest(self):
        # Fase 1 + Fase 11: apply com checklist 100% grava _hermes_state=ready
        # e o Ready Manifest (hash SHA-256) — nao e o latest.json que decide.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), root, 42, editorial_payload())
            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["state"], "ready")
            meta = client.post["meta"]
            self.assertEqual(meta["_hermes_state"], "ready")
            self.assertTrue(meta["_hermes_ready_hash"])
            self.assertTrue(meta["_hermes_ready_manifest"])
            # WP REST persiste meta como string (tipo 'string' registrado).
            self.assertEqual(meta["_hermes_policy_version"], "2")
            self.assertEqual(meta["_hermes_attempts"], "0")
            queue = build_queue_report(client, root)
            self.assertEqual(queue["edited"], 1)
            self.assertEqual(queue["ready_ids"], [42])

    def test_publish_ready_cheap_path_via_manifest(self):
        # Fase 11: nada mudou no WP desde o apply -> publica sem revalidar.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            apply_editorial(client, self.config(False), root, 42, editorial_payload())
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True,
            )
            outcome = publish_post(client, config, root, 42)
            self.assertEqual(outcome["status"], "published")
            self.assertEqual(outcome["integrity"], "manifest_match")
            self.assertEqual(len(client.updated), 2)  # apply + publish (sem revalidacao)

    def test_publish_ready_stale_revalidates_and_blocks(self):
        # Fase 11: conteudo mudou apos o apply (edicao externa) -> STALE ->
        # revalidacao completa; falha -> blocked (nunca publica quebrado).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            apply_editorial(client, self.config(False), root, 42, editorial_payload())
            client.post["content"]["raw"] = "<p>Conteudo alterado externamente.</p>"
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True,
            )
            outcome = publish_post(client, config, root, 42)
            self.assertEqual(outcome["status"], "blocked")
            self.assertGreater(outcome["checklist"]["failed"], 0)
            self.assertEqual(client.post["meta"]["_hermes_state"], "blocked")
            self.assertTrue((root / "backups/42/editorial.blocked.json").is_file())

    def test_publish_ready_only_processes_ready_or_legacy(self):
        # Fase 10: blocked/uncertain/awaiting_human nao entram no publish —
        # sem checklist caro para quem pertence a fila de rework.
        ready_post = self.checklist_pass_post()
        ready_post["id"] = 1
        blocked_post = dict(ready_post)
        blocked_post["id"] = 2
        blocked_post["meta"] = {"_hermes_state": "blocked", "_hermes_attempts": 1}
        uncertain_post = dict(ready_post)
        uncertain_post["id"] = 3
        uncertain_post["meta"] = {"_hermes_state": "uncertain"}

        class MultiClient(FakeClient):
            def __init__(self, posts):
                super().__init__(posts[0])
                self.posts = posts

            def get_post(self, post_id):
                return next(p for p in self.posts if p["id"] == post_id)

            def list_pending(self, per_page=100, page=1):
                return self.posts if page <= 1 else []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = MultiClient([ready_post, blocked_post, uncertain_post])
            # Post 1 passa pelo preflight (fica READY com manifest).
            apply_editorial(client, self.config(False), root, 1, editorial_payload())
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True,
            )
            outcomes = publish_ready_posts(client, config, root)
            published = [o for o in outcomes if o.get("wordpress_changed")]
            self.assertEqual([o["post_id"] for o in published], [1])
            # blocked/uncertain nem chegam ao publish_post (0 chamadas).
            publish_writes = [u for u in client.updated if u[1].get("status") == "publish"]
            self.assertEqual(len(publish_writes), 1)

    def test_queue_monitor_respects_rework_cooldown(self):
        # Fase 9: BLOCKED em cooldown (next_retry_at futuro) nao e elegivel;
        # vencido/legado e elegivel e entra na linha do monitor.
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        in_cooldown = self.post()
        in_cooldown["id"] = 1
        in_cooldown["meta"] = {
            "_hermes_state": "blocked",
            "_hermes_attempts": 1,
            "_hermes_next_retry_at": (now + datetime.timedelta(hours=2)).isoformat(),
        }
        eligible = self.post()
        eligible["id"] = 2
        eligible["meta"] = {
            "_hermes_state": "blocked",
            "_hermes_attempts": 1,
            "_hermes_next_retry_at": (now - datetime.timedelta(minutes=5)).isoformat(),
        }

        class MultiClient(FakeClient):
            def __init__(self, posts):
                super().__init__(posts[0])
                self.posts = posts

            def list_pending(self, per_page=50, page=1):
                return self.posts if page <= 1 else []

        with tempfile.TemporaryDirectory() as directory:
            report = build_queue_report(MultiClient([in_cooldown, eligible]), Path(directory))
            self.assertEqual(report["blocked"], 2)
            self.assertEqual(report["eligible_rework_ids"], [2])
            self.assertEqual(report["blocked_ids"], [1, 2])

    def test_retry_post_resets_attempts_and_cooldown(self):
        # Fase 13 (minimo): revisao humana reabre AWAITING_HUMAN/BLOCKED sem
        # forcar READY — zera tentativas e cooldown, mantem blocked.
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            result = retry_post(client, self.config(False), Path(directory), 42)
            self.assertEqual(result["status"], "retried")
            self.assertEqual(result["state"], "blocked")
            meta = client.post["meta"]
            self.assertEqual(meta["_hermes_state"], "blocked")
            self.assertEqual(meta["_hermes_attempts"], "0")
            self.assertEqual(meta["_hermes_next_retry_at"], "")

    def test_discard_post_marks_uncertain_and_leaves_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            result = discard_post(client, self.config(False), root, 42, reason="sem imagens reais")
            self.assertEqual(result["status"], "discarded")
            self.assertEqual(client.post["meta"]["_hermes_state"], "uncertain")
            self.assertTrue((root / "backups/42/uncertain.json").is_file())
            queue = build_queue_report(client, root)
            self.assertEqual(queue["uncertain"], 1)
            self.assertIn(42, queue["uncertain_ids"])

    def test_validate_media_plan_rejects_duplicate_source(self):
        # Politica anti-repeticao: a MESMA fonte nao pode entrar duas vezes
        # no media_plan (o "mesma imagem no post inteiro" da producao). O
        # segundo item com a mesma URL e rejeitado ANTES do apply.
        item = {
            "paragraph_index": 1,
            "source_page_url": "https://source.example/titulo",
            "direct_image_url": "https://cdn.example/titulo.jpg",
            "author": "Autor",
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0",
            "captured_at": "2026-08-20T12:00:00Z",
            "credit_text": "Crédito da imagem: Autor. Título do jogo. Licença CC BY 4.0 (https://creativecommons.org/licenses/by/4.0).",
            "alt_text": "Titulo do jogo key art",
            "is_featured": False,
        }
        editorial = editorial_payload()
        editorial["media_plan"] = [dict(item), dict(item)]
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            result = validate_media_plan(client, editorial)
        self.assertEqual(result["valid"], 1)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertIn("repetida", result["rejected"][0]["reason"])

    def test_apply_rejects_duplicate_media_plan_source(self):
        # O apply tambem rejeita a segunda ocorrencia da mesma fonte (o card
        # nao insere a mesma imagem duas vezes).
        payload = editorial_payload()
        payload["cleaned_html"] = (
            "<p>Um videogame.</p><p>Dois.</p><p>Tres.</p><p>Quatro.</p>"
            "<p>Cinco.</p><p>Seis.</p><p>Sete.</p>"
        )
        base = {
            "source_page_url": "https://source.example/titulo",
            "direct_image_url": "https://source.example/titulo.jpg",
            "author": "Autor",
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "captured_at": "2026-08-20T12:00:00Z",
            "credit_text": "Crédito da imagem: Autor. Imagem do titulo do jogo. Licença CC BY 4.0 (https://creativecommons.org/licenses/by/4.0).",
            "alt_text": "Imagem do titulo do jogo",
            "is_featured": False,
        }
        payload["media_plan"] = [
            {**base, "paragraph_index": 0},
            {**base, "paragraph_index": 3},
        ]
        with mock.patch("unicornio_editor.workflow.download_image", return_value=Path("/tmp/source.jpg")), mock.patch(
            "unicornio_editor.workflow.convert_to_webp", return_value=Path("/tmp/inline.webp")
        ), mock.patch(
            "unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "teste")
        ), mock.patch(
            "unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)
        ), mock.patch(
            "unicornio_editor.workflow.upload_image",
            side_effect=[
                {"id": 50, "source_url": "https://wp.test/50.webp"},
            ],
        ):
            with tempfile.TemporaryDirectory() as directory:
                client = FakeClient(self.post())
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        # O segundo item (fonte duplicada) e rejeitado; nao entra no conteudo.
        # Resultado de sucesso carrega media_id; rejeitado carrega status.
        self.assertEqual(len(report["media_plan_results"]), 2)
        rejected = [r for r in report["media_plan_results"] if r.get("status") == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("repetida", rejected[0]["detail"])


    def test_apply_writes_blocked_telemetry(self):
        # Telemetria central: um apply que falha no checklist grava
        # work/telemetry.jsonl com apply_blocked + motivo, para o operador
        # responder "a fila parou por que?" sem abrir backups.
        from unicornio_editor.observability import read_telemetry_summary

        payload = editorial_payload()
        # Conteudo sem imagens -> imagens_no_corpo falha -> needs_rework.
        payload["cleaned_html"] = "<p>Texto sobre videogame sem imagem.</p>"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), root, 42, payload)
            self.assertEqual(report["status"], "needs_rework")
            summary = read_telemetry_summary(root)
            self.assertEqual(summary["by_event"].get("apply_blocked"), 1)
            reasons = summary["by_reason"]["apply_blocked"]
            self.assertTrue(any("imagens" in r for r in reasons))


if __name__ == "__main__":
    unittest.main()
