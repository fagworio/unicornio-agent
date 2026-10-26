import datetime
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from unicornio_editor.config import Config
from unicornio_editor.manifest import manifest_matches, parse_manifest
from unicornio_editor.media.visual_identity import VisualIdentity, VisualIdentityDecision
from unicornio_editor.media.license import attest_source_rights as _attest_source_rights
from unicornio_editor.pipeline_v2.model import (
    BlockerCode,
    FeaturedProgress,
    FeaturedStatus,
    InlineMedia,
    LifecycleState,
    MediaProgress,
    Phase,
    WorkState,
)
from unicornio_editor.workflow import (
    WorkflowError,
    _load_partial_manifest,
    _publish_v2_media_context,
    _partial_retry,
    _recover_partial_featured,
    _execute_media_plan,
    _is_final_phash_duplicate,
    _item_entities,
    _media_item_rejection,
    _vision_evidence_matches_item,
    apply_editorial,
    build_cards,
    build_queue_report,
    discard_post,
    get_cleaned_content,
    load_draft,
    mark_uncertain,
    prepare_post,
    publish_post,
    publish_ready_posts,
    retry_post,
    validate_media_plan,
)
from unicornio_editor.wordpress import WordPressError


def _verified_media_identity():
    return VisualIdentity("a" * 64, "1111111111111111", "visual:" + "a" * 24), VisualIdentityDecision(
        "INITIAL", True, reason="test"
    )


def _write_test_vision_source(_url, destination, **_kwargs):
    from PIL import Image

    target = Path(destination)
    Image.new("RGB", (1280, 720), "#2457a6").save(target, format="PNG")
    return target


def _vision_result(*, accepted, status="MATCH", confidence=0.91, visual_type="key_art"):
    return {
        "accepted": accepted,
        "status": status,
        "confidence": confidence,
        "visual_type": visual_type,
        "verdict": "accept" if accepted else "reject",
        "reason": "modelo confirmou" if accepted else "imagem e share card",
        "detail_used": "low",
    }


def _mock_source_rights():
    """Keep executor unit tests offline; source-rights behavior has focused tests."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(mock.patch(
        "unicornio_editor.workflow.source_page_html", return_value="<html></html>"
    ))
    stack.enter_context(mock.patch(
        "unicornio_editor.media.license.attest_source_rights",
        return_value={"version": 1, "signature": "0" * 64},
    ))
    return stack


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
        if hasattr(self, "posts"):
            self.post = next(post for post in self.posts if post["id"] == post_id)
        payload = {"status": "publish"}
        if date_gmt:
            payload["date_gmt"] = date_gmt
        if meta:
            payload["meta"] = meta
        self.post["status"] = "publish"
        if date_gmt:
            self.post["date_gmt"] = date_gmt
        if meta:
            self.post.setdefault("meta", {}).update(meta)
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
    def setUp(self):
        def attest_test_source(candidate, _html):
            image_object = {
                "@context": "https://schema.org",
                "@type": "ImageObject",
                "contentUrl": candidate.get("direct_image_url"),
                "license": candidate.get("license_url"),
            }
            return _attest_source_rights(
                candidate,
                '<script type="application/ld+json">'
                + json.dumps(image_object)
                + "</script>",
            )

        self._source_html_patcher = mock.patch(
            "unicornio_editor.workflow.source_page_html", return_value="<html></html>"
        )
        self._source_html_patcher.start()
        self.addCleanup(self._source_html_patcher.stop)
        self._rights_attestation_patcher = mock.patch(
            "unicornio_editor.media.license.attest_source_rights",
            side_effect=attest_test_source,
        )
        self._rights_attestation_patcher.start()
        self.addCleanup(self._rights_attestation_patcher.stop)

    def config(self, dry_run):
        return Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=dry_run)

    def _attach_v2_ready_media(self, client, *, duplicate=False):
        featured_url = client.get_media(7)["source_url"]
        featured = FeaturedProgress(
            FeaturedStatus.VALID,
            media_id=7,
            media_url=featured_url,
            sha256="f" * 64,
            phash="f" * 16,
            visual_group_id="visual:featured",
            visual_verification={"decision": "INITIAL"},
        )
        inline = tuple(
            InlineMedia(
                media_id=8 + slot,
                media_url=f"https://s3.example/noticia-importante-{slot + 1}.webp",
                slot=slot,
                sha256=str(slot + 1) * 64,
                phash=str(slot + 1) * 16,
                visual_group_id=f"visual:inline:{slot}",
                visual_verification=(
                    {"decision": "SAME_ART_CROP", "duplicate_of": "7"}
                    if duplicate and slot == 1
                    else {"decision": "DIFFERENT"}
                ),
            )
            for slot in range(2)
        )
        state = WorkState(
            state=LifecycleState.READY,
            phase=Phase.VALIDATE,
            relevance_approved=True,
            media=MediaProgress(required=2, inline=inline, featured=featured),
        )
        client.post["meta"]["_hermes_work_state"] = json.dumps(state.to_dict())

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

    def test_partial_featured_is_recovered_from_manifest_not_accepted_media(self):
        client = FakeClient(self.post())
        featured_id, credit = _recover_partial_featured(
            client,
            42,
            {
                "state": "partial",
                "kind": "media",
                "accepted_media": [{"media_id": 123, "media_url": "https://wp.test/inline.webp"}],
                "featured": {
                    "status": "valid",
                    "media_id": 456,
                    "media_url": "https://wp.test/featured.webp",
                    "credit_text": "Crédito da featured",
                },
            },
        )
        self.assertEqual(featured_id, 456)
        self.assertEqual(credit, "Crédito da featured")

        now = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)
        first = _partial_retry(0, cooldown_minutes=30, max_no_progress_attempts=2, now=now)
        second = _partial_retry(1, cooldown_minutes=30, max_no_progress_attempts=2, now=now)
        terminal = _partial_retry(2, cooldown_minutes=30, max_no_progress_attempts=2, now=now)
        self.assertEqual(first["state"], "partial")
        self.assertEqual(second["state"], "partial")
        self.assertEqual(first["next_retry_at"], "2026-10-01T00:30:00+00:00")
        self.assertEqual(second["next_retry_at"], "2026-10-01T02:00:00+00:00")
        self.assertEqual(terminal, {"state": "awaiting_human", "next_retry_at": ""})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "backups" / "42" / "editorial.partial.json"
            path.parent.mkdir(parents=True)
            path.write_text("{invalid", encoding="utf-8")
            with self.assertRaisesRegex(WorkflowError, "partial_manifest_invalid"):
                _load_partial_manifest(root, 42, required=True)

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
            # Quando o bloqueio vira uncertain e nao ha outro trabalho,
            # monitoramos a segunda passagem automaticamente.
            (root / "backups" / "42" / "uncertain.json").write_text(
                json.dumps({"status": "uncertain"}), encoding="utf-8"
            )
            report = build_queue_report(client, root)
            self.assertEqual(report["blocked"], 0)
            self.assertEqual(report["uncertain"], 1)
            self.assertEqual(report["blocked_ids"], [])
            self.assertEqual(report["eligible_rework_ids"], [42])

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

    def test_apply_does_not_inherit_rank_math_keyword_absent_from_body(self):
        from unicornio_editor.editorial_schema import EditorialValidationError

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
            with self.assertRaises(EditorialValidationError):
                apply_editorial(FakeClient(post), self.config(True), Path(directory), 42, payload)
            self.assertFalse(Path(directory, "backups/42/editorial.latest.json").exists())

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
            conteudo_antes = client.post["content"]["raw"]
            report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
            self.assertEqual(report["status"], "uncertain")
            # UNCERTAIN nao altera conteudo: a duvida e sobre pertencer ao portal.
            self.assertFalse(report["wordpress_changed"])
            self.assertFalse(report["baseline_enriched"])
            self.assertEqual(client.post["content"]["raw"], conteudo_antes)
            self.assertNotIn("Confira mais novidades", client.post["content"]["raw"])
            self.assertTrue(Path(directory, "backups/42/uncertain.json").is_file())
            self.assertFalse(Path(directory, "backups/42/editorial.latest.json").exists())

    def test_uncertain_second_cycle_escalates_to_awaiting_human(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = editorial_payload("skip")
            payload["site_relevance"]["confidence"] = 0.70
            client = FakeClient(self.post())
            first = apply_editorial(client, self.config(False), Path(directory), 42, payload)
            second = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        self.assertEqual(first["state"], "uncertain")
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(second["state"], "awaiting_human")
        self.assertEqual(second["attempts"], 2)

    def test_apply_confident_skip_is_final(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = editorial_payload("skip")
            payload["site_relevance"]["confidence"] = 0.99
            client = FakeClient(self.post())
            conteudo_antes = client.post["content"]["raw"]
            report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
            # SKIPPED = zero alteracao de conteudo no WordPress (contrato do
            # README: post fora da pauta e ignorado sem alterar o site).
            self.assertFalse(report["wordpress_changed"])
            self.assertFalse(report["baseline_enriched"])
            self.assertEqual(client.post["content"]["raw"], conteudo_antes)
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
            eligible = self.post()
            eligible["title"] = {"raw": "Uncertain volta ao trabalho"}
            eligible["meta"] = {
                "_hermes_state": "uncertain",
                "_hermes_attempts": "1",
            }
            in_cooldown = dict(eligible)
            in_cooldown["id"] = 43
            in_cooldown["title"] = {"raw": "Uncertain em cooldown fica fora"}
            in_cooldown["meta"] = {
                "_hermes_state": "uncertain",
                "_hermes_attempts": "1",
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
            # Sem NEW/BLOCKED elegivel, o proximo lote e' o UNCERTAIN elegivel;
            # cooldown continua protegido.
            self.assertEqual(ids, [42])
            self.assertEqual(report["cards"][0]["retry_mode"], "uncertain_second_pass")

    def test_queue_marks_only_explicit_uncertain_second_pass_eligibility(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            post = self.post()
            post["meta"] = {"_hermes_state": "uncertain", "_hermes_attempts": "1"}
            report = build_queue_report(FakeClient(post), root)
            self.assertIs(type(report["uncertain_second_pass_eligible"]), int)
            self.assertEqual(report["uncertain_second_pass_eligible"], 1)
            self.assertEqual(report["uncertain_second_pass_ids"], [42])
            self.assertTrue(report["posts"][0]["uncertain_second_pass_eligible"])

    def test_cards_reserve_uncertain_capacity_before_new_and_preserve_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uncertain = self.post()
            uncertain["meta"] = {
                "_hermes_state": "uncertain",
                "_hermes_attempts": "1",
                "_hermes_last_error": "motivo anterior",
            }
            new = dict(self.post())
            new["id"] = 43

            class Two(FakeClient):
                def list_pending(self, **kwargs):
                    return [uncertain, new]

            config = replace(self.config(True), uncertain_second_pass_limit=1)
            report = build_cards(Two(uncertain), config, root, per_page=1)
            self.assertEqual([card["id"] for card in report["cards"]], [42])
            card = report["cards"][0]
            self.assertEqual(card["previous_relevance_reason"], "motivo anterior")
            self.assertEqual(card["next_retry_at"], "")
            self.assertEqual(report["uncertain_second_pass_reserved"], 1)

    def test_uncertain_second_pass_requires_pending_and_attempt_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_attempt = self.post()
            bad_attempt["meta"] = {"_hermes_state": "uncertain", "_hermes_attempts": "2"}
            wrong_status = dict(self.post())
            wrong_status["id"] = 43
            wrong_status["status"] = "publish"
            wrong_status["meta"] = {"_hermes_state": "uncertain", "_hermes_attempts": "1"}

            class Two(FakeClient):
                def list_pending(self, **kwargs):
                    return [bad_attempt, wrong_status]

            report = build_queue_report(Two(bad_attempt), root)
            self.assertFalse(report["uncertain_second_pass_eligible"])
            self.assertEqual(report["uncertain_second_pass_ids"], [])

    def test_build_cards_does_not_reintroduce_blocked_cooldown_from_general_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            future = (
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
            ).isoformat(timespec="seconds")
            blocked = self.post()
            blocked["meta"] = {
                "_hermes_state": "blocked",
                "_hermes_attempts": "1",
                "_hermes_next_retry_at": future,
            }
            (root / "backups" / "42").mkdir(parents=True)
            (root / "backups" / "42" / "editorial.blocked.json").write_text(
                json.dumps({"status": "blocked"}), encoding="utf-8"
            )

            class BlockedClient(FakeClient):
                def list_pending(self, **kwargs):
                    return [blocked]

            report = build_cards(BlockedClient(blocked), self.config(True), root)
            self.assertEqual(report["cards"], [])

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

    def test_media_funnel_events_keep_post_context(self):
        payload = editorial_payload()
        payload["media_plan"] = [{
            **self.media_item(),
            "subject": "videogame",
            "search_query": "videogame lançamento",
            "engine": "duckduckgo",
            "candidate_id": "42-candidate-1",
        }]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validate_media_plan(FakeClient(self.post()), payload, root=root, post_id=42)
            events = [
                json.loads(line)
                for line in (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("event") == "media_funnel"
            ]
        assert events
        assert all(event["post_id"] == 42 for event in events)
        assert all(event["query"] == "videogame lançamento" for event in events)
        assert all(event["subject"] == "videogame" for event in events)
        assert all(event["engine"] == "duckduckgo" for event in events)
        assert all("direct_image_url" not in event for event in events)

    def test_media_validate_reports_listicle_capacity_without_counting_featured(self):
        payload = editorial_payload()
        payload["seo"]["title"] = "4 melhores jogos para fãs de Redfall"
        payload["cleaned_html"] = "".join(
            f"<h2>{n}. Jogo {n}: destaque</h2><p>Videogame e lançamento.</p>"
            for n in range(1, 5)
        )
        payload["media_plan"] = [
            {
                **self.media_item(paragraph_index=index),
                "direct_image_url": f"https://source.example/redfall-{index}.jpg",
                "source_page_url": f"https://source.example/redfall-{index}",
                "search_query": "Redfall jogos",
                "is_featured": index == 3,
            }
            for index in range(4)
        ]
        result = validate_media_plan(FakeClient(self.post()), payload)
        self.assertTrue(result["listicle"]["applicable"])
        self.assertFalse(result["listicle"]["feasible"])
        self.assertEqual(result["listicle"]["verified_inline_capacity"], 3)
        self.assertEqual(result["listicle"]["missing"], 1)
        self.assertEqual(result["listicle"]["missing_items"], [4])

    def test_media_validate_rejects_featured_denied_by_vision(self):
        payload = editorial_payload()
        payload["media_plan"] = [self.media_item(paragraph_index=0, is_featured=True)]
        config = Config(
            "wordpress", "http://wp.test", "/wp-json/wp/v2",
            vision_enabled=True, vision_api_key="test-key", vision_base_url="https://vision.test/v1",
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "unicornio_editor.workflow.download_image", side_effect=_write_test_vision_source,
        ), mock.patch(
            "unicornio_editor.workflow.verify_image_subject_decision",
            return_value=_vision_result(accepted=False, status="UNRELATED", confidence=0.92),
        ):
            result = validate_media_plan(
                FakeClient(self.post()), payload, config=config, root=Path(directory)
            )
        self.assertEqual(result["valid"], 0)
        self.assertEqual(result["featured_vision"][0]["status"], "rejected")
        self.assertIn("share card", result["rejected"][0]["reason"])
        stored = result["featured_vision"][0]["vision_evidence"]
        self.assertEqual(stored["status"], "UNRELATED")
        self.assertEqual(stored["confidence"], 0.92)
        self.assertEqual(stored["visual_type"], "key_art")
        self.assertEqual(stored["image_role"], "featured")
        self.assertEqual(stored["policy_version"], "featured-editorial-v4-context-scoped")
        self.assertRegex(stored["image_sha256"], r"^[a-f0-9]{64}$")

    def test_media_validate_skips_paid_vision_for_strong_official_featured_evidence(self):
        payload = editorial_payload()
        payload["seo"]["title"] = "Blue Box temporada 2"
        payload["media_plan"] = [{
            **self.media_item(paragraph_index=0, is_featured=True),
            "direct_image_url": "https://image.tmdb.org/t/p/original/abc123.jpg",
            "source_page_url": "https://anime.com/shows/blue-box",
            "search_query": "Blue Box temporada 2 anime",
        }]
        config = Config(
            "wordpress", "http://wp.test", "/wp-json/wp/v2",
            vision_enabled=True, vision_api_key="test-key", vision_base_url="https://vision.test/v1",
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "unicornio_editor.workflow.download_image", side_effect=_write_test_vision_source,
        ), mock.patch(
            "unicornio_editor.workflow.verify_image_subject_decision"
        ) as vision:
            vision.return_value = _vision_result(accepted=True)
            result = validate_media_plan(
                FakeClient(self.post()), payload, config=config, root=Path(directory)
            )
        vision.assert_not_called()
        self.assertEqual(result["valid"], 1)
        self.assertTrue(result["featured_vision"][0]["deterministic"])

    def test_media_validate_keeps_paid_vision_for_untrusted_featured_source(self):
        payload = editorial_payload()
        payload["media_plan"] = [{
            **self.media_item(paragraph_index=0, is_featured=True),
            "direct_image_url": "https://cdn.example/keyart.jpg",
            "source_page_url": "https://news.example/redfall",
            "search_query": "videogame lancamento importante",
        }]
        config = Config(
            "wordpress", "http://wp.test", "/wp-json/wp/v2",
            vision_enabled=True, vision_api_key="test-key", vision_base_url="https://vision.test/v1",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "unicornio_editor.workflow.download_image", side_effect=_write_test_vision_source,
            ), mock.patch(
                "unicornio_editor.workflow.verify_image_subject_decision",
                return_value=_vision_result(accepted=True),
            ) as vision:
                result = validate_media_plan(FakeClient(self.post()), payload, config=config, root=root)
            from unicornio_editor.media.vision_cache import get_cached_decision
            from unicornio_editor.media.vision_cache import vision_model_identity
            from unicornio_editor.media.vision_policy import vision_cache_subject
            from unicornio_editor.media.vision_policy import featured_vision_context_key

            vision_row = result["featured_vision"][0]
            image_sha256 = vision_row["image_sha256"]
            cached = get_cached_decision(
                root, payload["media_plan"][0]["direct_image_url"],
                vision_cache_subject(payload), image_sha256=image_sha256,
                policy="featured-key-art-v1",
                model_identity=vision_model_identity(config.vision_base_url, config.vision_model),
                context_key=featured_vision_context_key(payload),
            )
            self.assertEqual(vision_row["decision"]["confidence"], 0.91)
            self.assertEqual(vision_row["decision"]["visual_type"], "key_art")
            self.assertEqual(cached["confidence"], 0.91)
            self.assertEqual(cached["visual_type"], "key_art")
        vision.assert_called_once()
        self.assertTrue(vision.call_args.kwargs["image_url"].startswith("data:image/webp;base64,"))

    def test_media_validate_checks_existing_featured_when_plan_has_none(self):
        config = Config(
            "wordpress", "http://wp.test", "/wp-json/wp/v2",
            vision_enabled=True, vision_api_key="test-key", vision_base_url="https://vision.test/v1",
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "unicornio_editor.workflow.download_image", side_effect=_write_test_vision_source,
        ), mock.patch(
            "unicornio_editor.workflow.verify_image_subject_decision",
            return_value=_vision_result(accepted=True),
        ) as vision:
            result = validate_media_plan(
                FakeClient(self.post()),
                editorial_payload(),
                config=config,
                root=Path(directory),
                existing_featured_id=7,
            )
        vision.assert_called_once()
        self.assertTrue(vision.call_args.kwargs["image_url"].startswith("data:image/webp;base64,"))
        self.assertEqual(result["featured_vision"][0]["existing_featured_id"], 7)
        self.assertEqual(result["featured_vision"][0]["status"], "passed")

    def test_apply_skip_nao_altera_conteudo_no_wordpress(self):
        """SKIPPED = zero alteracao de conteudo (antes persistia CTA/Fonte).

        O commit b0d35d8 fazia skip/uncertain/discard persistirem melhorias
        mecanicas (CTA, Fonte, links) no conteudo do WordPress. Isso contradizia
        o contrato de seguranca do README: o motivo do skip pode ser justamente
        o conteudo nao pertencer ao portal. Agora o skip grava APENAS a meta de
        estado (para o post sair da fila), nunca o conteudo.
        """
        with tempfile.TemporaryDirectory() as directory:
            post = self.post()
            original = "<h1>Titulo duplicado</h1><p>Notícia sobre Xbox.</p>"
            post["content"] = {"raw": original}
            client = FakeClient(post)
            report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload("skip"))
            self.assertFalse(report["wordpress_changed"])
            self.assertEqual(report["status"], "skipped")
            # exatamente UMA escrita: a meta _hermes_state (nunca o conteudo)
            self.assertEqual(len(client.updated), 1)
            self.assertIn("meta", client.updated[0][1])
            self.assertNotIn("content", client.updated[0][1])
            self.assertEqual(client.updated[0][1]["meta"]["_hermes_state"], "skipped")
            self.assertEqual(client.post["content"]["raw"], original)

    def test_apply_processes_pending_without_sending_status(self):
        with tempfile.TemporaryDirectory() as directory:
            post = self.post()
            post["meta"].update({
                "_hermes_human_reopened_at": "2030-01-01T00:00:00+00:00",
                "_hermes_human_reopened_from": "awaiting_human",
                "rank_math_robots": ["index"],
            })
            client = FakeClient(post)
            report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload())
            self.assertTrue(report["wordpress_changed"])
            self.assertNotIn("status", client.updated[0][1])
            sent_meta = client.updated[0][1]["meta"]
            self.assertNotIn("_hermes_human_reopened_at", sent_meta)
            self.assertNotIn("_hermes_human_reopened_from", sent_meta)
            self.assertNotIn("rank_math_robots", sent_meta)
            self.assertEqual(client.post["meta"]["_hermes_human_reopened_from"], "awaiting_human")
            self.assertEqual(client.post["meta"]["rank_math_robots"], ["index"])
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
        with mock.patch("unicornio_editor.workflow.find_game_trailer_with_status", return_value=(trailer, "found")):
            with tempfile.TemporaryDirectory() as directory:
                client = FakeClient(self.post())
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        raw = client.updated[0][1]["content"]["raw"]
        self.assertIn("https://www.youtube-nocookie.com/embed/abcDEF12345", raw)
        self.assertIn("Confira mais novidades", raw)
        self.assertLess(raw.index("youtube-nocookie"), raw.index("Confira mais novidades"))
        self.assertEqual(report["trailer"]["video_id"], "abcDEF12345")

    def test_apply_without_game_name_does_not_search(self):
        with mock.patch("unicornio_editor.workflow.find_game_trailer_with_status") as discovery:
            with tempfile.TemporaryDirectory() as directory:
                client = FakeClient(self.post())
                report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload())
        discovery.assert_not_called()
        self.assertIsNone(report["trailer"])

    def test_apply_persists_audited_waiver_when_official_trailer_is_absent(self):
        payload = editorial_payload()
        payload["game_name"] = "Jogo sem trailer"
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "unicornio_editor.workflow.find_game_trailer_with_status",
            return_value=(None, "official_not_found"),
        ):
            root = Path(directory)
            report = apply_editorial(FakeClient(self.post()), self.config(False), root, 42, payload)
            saved = json.loads((root / "backups/42/editorial.latest.json").read_text())
        self.assertEqual(report["status"], "ready")
        self.assertTrue(saved["trailer_unavailable"])
        self.assertEqual(saved["trailer_search_evidence"]["result"], "official_not_found")

    def test_apply_does_not_waive_trailer_when_search_failed(self):
        payload = editorial_payload()
        payload["game_name"] = "Jogo sem trailer"
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "unicornio_editor.workflow.find_game_trailer_with_status",
            return_value=(None, "search_failed"),
        ):
            report = apply_editorial(
                FakeClient(self.post()), self.config(False), Path(directory), 42, payload
            )
        self.assertEqual(report["status"], "needs_rework")
        self.assertIn("trailer_youtube", report["blocked_reasons"])

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
        with mock.patch("unicornio_editor.workflow.find_game_trailer_with_status", return_value=(trailer, "found")):
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
            "unicornio_editor.media.visual_identity.verify_candidate_identity",
            return_value=_verified_media_identity(),
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

    def test_unproven_image_rights_reject_candidate_before_upload(self):
        payload = editorial_payload()
        payload["cleaned_html"] = (
            "<p>Um videogame.</p><p>Dois.</p><p>Tres.</p><p>Quatro.</p>"
            "<p>Cinco.</p><p>Seis.</p><p>Sete.</p>"
        )
        payload["media_plan"] = [
            {**self.media_item(paragraph_index=0), "direct_image_url": "https://source.example/a.jpg"},
        ]
        with mock.patch(
            "unicornio_editor.workflow.download_image", return_value=Path("/tmp/source.jpg")
        ), mock.patch(
            "unicornio_editor.workflow.verify_downloaded_against_source",
            return_value=(True, "source_reference_found: test"),
        ), mock.patch(
            "unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)
        ), mock.patch(
            "unicornio_editor.media.license.attest_source_rights", return_value=None
        ), mock.patch("unicornio_editor.workflow.upload_image") as upload:
            with tempfile.TemporaryDirectory() as directory:
                report = apply_editorial(
                    FakeClient(self.post()), self.config(False), Path(directory), 42, payload
                )
        upload.assert_not_called()
        results = report["media_plan_results"]
        self.assertEqual(results[0]["reason_code"], "image_rights_unproven")

    def test_unproven_candidate_does_not_block_later_licensed_candidate(self):
        items = [
            {
                **self.media_item(paragraph_index=index * 3),
                "direct_image_url": f"https://source.example/{name}.jpg",
                "alt_text": f"Wolverine {name}",
            }
            for index, name in enumerate(("unproven", "licensed"))
        ]
        editorial = {
            "seo": {"title": "Wolverine videogame", "focus_keyword": "Wolverine"},
            "cleaned_html": "<p>Wolverine videogame.</p>",
            "media_plan": items,
        }
        config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
        with mock.patch("unicornio_editor.workflow._media_item_rejection", return_value=None), \
             mock.patch("unicornio_editor.workflow.download_image", return_value=Path("/tmp/source.jpg")), \
             mock.patch("unicornio_editor.workflow.convert_to_webp", return_value=Path("/tmp/inline.webp")), \
             mock.patch("unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "verified")), \
             mock.patch("unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)), \
             mock.patch("unicornio_editor.workflow.image_has_transparency", return_value=False), \
             mock.patch("unicornio_editor.workflow.image_is_mostly_flat", return_value=False), \
             mock.patch("unicornio_editor.media.visual_hash.phash_from_path", return_value="1111111111111111"), \
             mock.patch("unicornio_editor.media.visual_identity.verify_candidate_identity", return_value=_verified_media_identity()), \
             mock.patch(
                 "unicornio_editor.media.license.attest_source_rights",
                 side_effect=[None, {"version": 1, "signature": "0" * 64}],
             ), \
             mock.patch(
                 "unicornio_editor.workflow.upload_image",
                 return_value={"id": 77, "source_url": "https://wp.test/77.webp"},
             ) as upload:
            with tempfile.TemporaryDirectory() as directory:
                results, _, _ = _execute_media_plan(
                    editorial, config, object(), Path(directory),
                    preflight={"rejected": [], "featured_vision": []}, post_id=42,
                )
        self.assertEqual(results[0]["reason_code"], "image_rights_unproven")
        self.assertEqual(results[1]["status"], "accepted")
        upload.assert_called_once()

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
            "unicornio_editor.media.visual_identity.verify_candidate_identity",
            return_value=_verified_media_identity(),
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
        ), mock.patch(
            "unicornio_editor.media.visual_identity.verify_candidate_identity",
            return_value=_verified_media_identity(),
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
                client.post["meta"] = {"_hermes_media_search_attempts": "1"}
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        self.assertEqual(report["state"], "awaiting_human")
        self.assertEqual(report["status"], "needs_rework")
        # P2.4: o post e movido para o status WP que o humano filtra (e o
        # resultado do move e verificado depois da escrita - se o WP nao
        # confirmar, vira telemetria em vez de silencio).
        self.assertTrue(
            any(payload.get("status") == "awaiting_human" for _pid, payload in client.updated)
        )

    def test_awaiting_human_normalizes_existing_featured_before_human_review(self):
        post = self.post()
        post["featured_media"] = 7
        payload = editorial_payload()
        payload["media_exhausted"] = True
        payload["seo"] = {
            "title": "2 melhores jogos de videogame",
            "meta_description": "Uma descrição suficientemente longa sobre o conteúdo de videogame, seus detalhes, plataformas e contexto para o leitor entender a notícia.",
            "focus_keyword": "videogame",
        }
        payload["cleaned_html"] = (
            "<h2>1. Jogo: titulo</h2><p>Descricao do primeiro jogo.</p>"
            "<h2>2. Jogo: titulo</h2><p>Descricao do segundo jogo.</p>"
        )

        class MediaClient(FakeClient):
            def get_media(self, _media_id):
                return {
                    "id": 7,
                    "source_url": "https://wp.test/uploads/melhores-jogos-de-videogame.jpg",
                    "media_details": {"width": 1920, "height": 1080},
                    "alt_text": "Melhores jogos de videogame",
                    "title": {"rendered": "Melhores jogos de videogame"},
                    "caption": {"rendered": "Crédito da imagem: Autor."},
                }

            def upload_media(self, *_args, **_kwargs):
                return {"id": 88, "source_url": "https://wp.test/uploads/melhores-jogos-de-videogame-1280x720.webp"}

        with mock.patch("unicornio_editor.workflow.download_image", return_value=Path("/tmp/old.jpg")), mock.patch(
            "unicornio_editor.workflow.prepare_featured_webp", return_value=Path("/tmp/new.webp")
        ):
            with tempfile.TemporaryDirectory() as directory:
                client = MediaClient(post)
                client.post["meta"] = {"_hermes_media_search_attempts": "1"}
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        self.assertEqual(report["state"], "awaiting_human")
        self.assertEqual(client.post["featured_media"], 88)
        self.assertTrue(report["featured_normalized"])

    def test_article_sem_waiver_por_contagem_de_apply(self):
        """Acceptance 13: `attempts` de APPLY não é evidência de busca esgotada.

        Antes o 2º apply de um artigo sem imagens virava `ready` só porque o
        contador chegou ao teto — sem nenhuma busca de imagem ter sido esgotada
        (o apply pode ter falhado em SEO, trailer, etc.). Agora o waiver só
        existe com evidência do CÓDIGO; sem ela o post segue bloqueado até o
        teto de tentativas mandá-lo para decisão humana (não trava em loop).
        """
        payload = editorial_payload()
        payload["cleaned_html"] = "<p>Texto revisado sobre videogame.</p>"  # 0 imagens
        payload["media_plan"] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            first = apply_editorial(client, self.config(False), root, 42, payload)
            second = apply_editorial(client, self.config(False), root, 42, payload)
            third = apply_editorial(client, self.config(False), root, 42, payload)
        self.assertEqual(first["state"], "partial")
        self.assertEqual(first["attempts"], 0)
        self.assertEqual(second["state"], "partial")
        self.assertEqual(third["state"], "awaiting_human")

    def test_legacy_featured_vision_partial_resumes_without_unbound_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            post = self.post()
            post["meta"] = {
                "_hermes_state": "partial",
                "_hermes_attempts": "1",
                "_hermes_partial_kind": "media",
                "_hermes_media_required": "4",
                "_hermes_media_completed": "4",
                "_hermes_media_missing": "0",
                "_hermes_last_error": "imagens_visao: featured rejeitada",
            }
            manifest = {
                "state": "partial", "kind": "media", "required": 4,
                "completed": 4, "missing": 0,
                "accepted_media": [
                    {"media_id": 100 + i, "media_url": f"https://wp.test/{i}.webp", "paragraph_index": i * 3,
                     "alt_text": "inline", "credit_text": "Crédito da imagem: Teste.", "width": 800, "height": 450}
                    for i in range(4)
                ],
                "featured": {"status": "valid", "media_id": 7, "media_url": "https://wp.test/featured.webp"},
            }
            backup = root / "backups/42"
            backup.mkdir(parents=True)
            (backup / "editorial.partial.json").write_text(json.dumps(manifest), encoding="utf-8")
            checklist = {"all_passed": False, "failed": [{"name": "imagens_visao", "detail": "featured rejeitada"}], "items": [{"name": "imagens_visao", "status": "fail", "detail": "featured rejeitada"}]}
            payload = editorial_payload()
            payload["cleaned_html"] = "".join(f"<p>Parágrafo {i} sobre videogame.</p>" for i in range(12))
            with mock.patch("unicornio_editor.workflow._execute_media_plan", return_value=([], None, "")), \
                 mock.patch("unicornio_editor.workflow.validate_media_plan", return_value={"featured_vision": []}), \
                 mock.patch("unicornio_editor.workflow.run_pre_publish_checklist", return_value=checklist):
                report = apply_editorial(FakeClient(post), self.config(False), root, 42, payload)
            self.assertEqual(report["status"], "partial")
            saved = json.loads((backup / "editorial.partial.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["featured"]["status"], "vision_rejected")
            self.assertEqual(saved["partial_kind"], "featured_vision")
            # A obrigação atual vem do conteúdo efetivo; o required=4 antigo
            # serve apenas para drift e não pode inflar um draft curto.
            self.assertEqual(saved["completed"], 2)
            self.assertEqual(saved["missing"], 0)

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
            "unicornio_editor.media.visual_identity.verify_candidate_identity",
            return_value=_verified_media_identity(),
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

    def test_existing_irrelevant_featured_rejection_is_auditable(self):
        from unicornio_editor.workflow import _normalize_existing_featured

        class Client:
            def __init__(self):
                self.uploads = 0

            def get_media(self, media_id):
                return {
                    "id": media_id,
                    "source_url": "https://cdn.test/site-logo-banner.jpg",
                    "title": {"rendered": "Site logo banner"},
                    "alt_text": "Brand logo",
                    "media_details": {"width": 1200, "height": 900},
                }

            def upload_media(self, *_args, **_kwargs):
                self.uploads += 1
                raise AssertionError("irrelevant featured must not be uploaded")

        client = Client()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = _normalize_existing_featured(
                client,
                self.config(False),
                {"id": 71, "featured_media": 115287},
                {
                    "seo": {"title": "Hollow Knight recebe atualização importante"},
                    "cleaned_html": "<p>Hollow Knight videogame update news.</p>",
                    "game_name": "Hollow Knight",
                },
                root=root,
            )
            events = [
                json.loads(line)
                for line in (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertIsNone(result)
        self.assertEqual(client.uploads, 0)
        self.assertTrue(any(
            event.get("event") == "featured_normalization"
            and event.get("reason_code") == "existing_featured_not_relevant"
            and event.get("media_id") == 115287
            for event in events
        ))

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

    def test_publish_ready_contract_is_up_to_five_effective_publications(self):
        base_post = self.post()

        class QueueClient(FakeClient):
            def __init__(self, posts):
                super().__init__(posts[0] if posts else base_post)
                self.posts = posts

            def get_post(self, post_id):
                return next(p for p in self.posts if p["id"] == post_id)

            def list_pending(self, per_page=50, page=1):
                return self.posts if page == 1 else []

        for ready_count, expected_published, expected_remaining in (
            (0, 0, 0), (1, 1, 0), (2, 2, 0), (3, 3, 0),
            (4, 4, 0), (5, 5, 0), (6, 5, 1), (12, 5, 7),
        ):
            with self.subTest(ready_count=ready_count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                posts = []
                for post_id in range(1, ready_count + 1):
                    post = self.checklist_pass_post()
                    post["id"] = post_id
                    post["meta"] = {"_hermes_state": "ready"}
                    posts.append(post)
                client = QueueClient(posts)
                config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", publish_enabled=True, publish_limit=5)
                with mock.patch("unicornio_editor.workflow.publish_post", return_value={"wordpress_changed": True}):
                    outcomes = publish_ready_posts(client, config, root, limit=5)
                self.assertEqual(sum(bool(o.get("wordpress_changed")) for o in outcomes), expected_published)
                self.assertEqual(ready_count - len(outcomes), expected_remaining)

    def test_publish_limit_counts_effective_publications_not_examined_posts(self):
        class QueueClient(FakeClient):
            def __init__(self, posts):
                super().__init__(posts[0])
                self.posts = posts

            def get_post(self, post_id):
                return next(p for p in self.posts if p["id"] == post_id)

            def list_pending(self, per_page=50, page=1):
                return self.posts if page == 1 else []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            posts = []
            for post_id in range(1, 8):
                post = self.checklist_pass_post()
                post["id"] = post_id
                post["meta"] = {"_hermes_state": "ready"}
                posts.append(post)
            client = QueueClient(posts)
            config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", publish_enabled=True, publish_limit=5)
            failed_ids = {1, 2}

            def publish_side_effect(_client, _config, _root, post_id):
                return {"post_id": post_id, "wordpress_changed": post_id not in failed_ids}

            with mock.patch("unicornio_editor.workflow.publish_post", side_effect=publish_side_effect):
                outcomes = publish_ready_posts(client, config, root, limit=5)
            self.assertEqual(len(outcomes), 7)
            self.assertEqual(sum(bool(o.get("wordpress_changed")) for o in outcomes), 5)

    def test_four_ready_and_three_blocked_publish_four_without_waiting(self):
        class QueueClient(FakeClient):
            def __init__(self, posts):
                super().__init__(posts[0])
                self.posts = posts

            def get_post(self, post_id):
                return next(p for p in self.posts if p["id"] == post_id)

            def list_pending(self, per_page=50, page=1):
                return self.posts if page == 1 else []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            posts = []
            for post_id in range(1, 8):
                post = self.checklist_pass_post()
                post["id"] = post_id
                post["meta"] = {"_hermes_state": "ready" if post_id <= 4 else "blocked"}
                posts.append(post)
            client = QueueClient(posts)

            def publish_side_effect(_client, _config, _root, post_id):
                return {"post_id": post_id, "wordpress_changed": True}

            with mock.patch("unicornio_editor.workflow.publish_post", side_effect=publish_side_effect):
                outcomes = publish_ready_posts(client, self.config(True), root, limit=5)
            self.assertEqual(len(outcomes), 4)
            self.assertEqual(sum(bool(o.get("wordpress_changed")) for o in outcomes), 4)

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
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["partial"]["missing"], 2)
            self.assertFalse(report["wordpress_changed"])
            self.assertEqual(len(client.updated), 1)
            state_meta = client.updated[0][1]["meta"]
            self.assertEqual(state_meta["_hermes_state"], "partial")
            self.assertEqual(report["state"], "partial")
            self.assertTrue((root / "backups/42/editorial.latest.json").is_file())
            self.assertFalse((root / "backups/42/editorial.blocked.json").is_file())
            self.assertTrue((root / "backups/42/editorial.partial.json").is_file())
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
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["state"], "partial")
            # O conteúdo já cobre as duas imagens inline; a falha restante é
            # exclusivamente a featured ausente.
            self.assertEqual(report["partial"]["missing"], 0)
            self.assertEqual(client.updated[0][1]["meta"]["_hermes_state"], "partial")
            # O conteúdo incompleto não é gravado no WordPress.
            self.assertNotIn("content", client.updated[0][1])

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
            self.assertEqual(meta["_hermes_policy_version"], "3")
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
            self.assertTrue(client.post["meta"]["_hermes_ready_hash"])
            self.assertTrue(client.post["meta"]["_hermes_ready_manifest"])

    def test_publish_v2_ready_revalidates_visual_evidence_even_when_manifest_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            apply_editorial(client, self.config(False), root, 42, editorial_payload())
            self._attach_v2_ready_media(client, duplicate=True)

            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True,
            )
            self.assertTrue(
                manifest_matches(
                    client.post,
                    parse_manifest(client.post["meta"]["_hermes_ready_manifest"]),
                    client.post["meta"]["_hermes_ready_hash"],
                    policy_version=config.policy_version,
                )
            )
            outcome = publish_post(client, config, root, 42)

            self.assertEqual(outcome["status"], "blocked")
            failures = {
                item["name"] for item in outcome["checklist"]["items"]
                if item["status"] in {"fail", "error"}
            }
            self.assertIn("media_duplicate_confirmed", failures)
            self.assertFalse(any(payload.get("status") == "publish" for _, payload in client.updated))

    def test_publish_checklist_failure_reopens_v2_ready_for_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            apply_editorial(client, self.config(False), root, 42, editorial_payload())
            self._attach_v2_ready_media(client)
            client.post["content"]["raw"] = "<p>Conteúdo alterado sem as imagens aceitas.</p>"
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True,
            )

            outcome = publish_post(client, config, root, 42)

            self.assertEqual(outcome["status"], "blocked")
            self.assertTrue(outcome["v2_reopened"])
            self.assertEqual(client.post["meta"]["_hermes_state"], "blocked")
            reopened = WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"]))
            self.assertIs(reopened.state, LifecycleState.PENDING)
            self.assertIs(reopened.blocker, BlockerCode.INLINE_MISSING)
            self.assertEqual(reopened.phase, Phase.MEDIA)
            from unicornio_editor.pipeline_v2.operational import WordPressStateBackend
            from unicornio_editor.pipeline_v2.scheduler import select
            from unicornio_editor.pipeline_v2.state_store import StateStore

            selected = select(
                [(42, client.post)],
                StateStore(WordPressStateBackend(client)),
                limit=1,
            )
            self.assertEqual([item[0] for item in selected], [42])
            self.assertFalse(any(payload.get("status") == "publish" for _, payload in client.updated))

    def test_publish_retryable_check_failure_reopens_v2_without_legacy_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            apply_editorial(client, self.config(False), root, 42, editorial_payload())
            self._attach_v2_ready_media(client)
            checklist = {
                "failed": 1,
                "items": [{
                    "name": "featured_identity_matches_current_attachment",
                    "status": "error",
                    "retryable": True,
                    "blocker": "provider_error",
                    "phase": "media_validation",
                }],
            }
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True,
            )
            with mock.patch(
                "unicornio_editor.workflow.run_pre_publish_checklist",
                return_value=checklist,
            ):
                outcome = publish_post(client, config, root, 42)

            self.assertEqual(outcome["status"], "retry")
            self.assertTrue(outcome["v2_reopened"])
            self.assertEqual(client.post["meta"]["_hermes_state"], "ready")
            reopened = WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"]))
            self.assertIs(reopened.state, LifecycleState.PENDING)
            self.assertIs(reopened.blocker, BlockerCode.PROVIDER_ERROR)
            self.assertEqual(reopened.phase, Phase.MEDIA)
            self.assertFalse(any(payload.get("status") == "publish" for _, payload in client.updated))

    def test_publish_reports_legacy_marker_failure_after_v2_reopen(self):
        class LegacyMarkerFailureClient(FakeClient):
            def update_post(self, post_id, payload):
                if (payload.get("meta") or {}).get("_hermes_state") == "blocked":
                    raise RuntimeError("legacy marker unavailable")
                return super().update_post(post_id, payload)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = LegacyMarkerFailureClient(self.post())
            apply_editorial(client, self.config(False), root, 42, editorial_payload())
            self._attach_v2_ready_media(client)
            client.post["content"]["raw"] = "<p>Conteúdo alterado sem as imagens aceitas.</p>"
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True,
            )

            outcome = publish_post(client, config, root, 42)

            self.assertEqual(outcome["status"], "retry")
            self.assertFalse(outcome["legacy_state_persisted"])
            reopened = WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"]))
            self.assertIs(reopened.state, LifecycleState.PENDING)
            self.assertFalse(any(payload.get("status") == "publish" for _, payload in client.updated))

    def test_publish_v2_ready_with_valid_media_passes_real_checklist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            apply_editorial(client, self.config(False), root, 42, editorial_payload())
            self._attach_v2_ready_media(client)
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True,
            )
            self.assertTrue(
                manifest_matches(
                    client.post,
                    parse_manifest(client.post["meta"]["_hermes_ready_manifest"]),
                    client.post["meta"]["_hermes_ready_hash"],
                    policy_version=config.policy_version,
                )
            )

            outcome = publish_post(client, config, root, 42)

            failed_items = [
                item for item in outcome.get("checklist", {}).get("items", [])
                if item.get("status") in {"fail", "error"}
            ]
            self.assertEqual(outcome["status"], "published", failed_items)
            self.assertEqual(outcome["integrity"], "revalidated")
            self.assertTrue(any(payload.get("status") == "publish" for _, payload in client.updated))

    def test_v2_context_keeps_accepted_media_valid_when_post_has_an_extra_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            apply_editorial(client, self.config(False), root, 42, editorial_payload())
            self._attach_v2_ready_media(client)
            state = json.loads(client.post["meta"]["_hermes_work_state"])
            client.post["content"]["raw"] += (
                '<figure><img src="https://s3.example/editor-added.webp" '
                'alt="Título sobre videogame e lançamento importante" /></figure>'
            )

            context = _publish_v2_media_context(state, client.post)

            self.assertIsNotNone(context)
            self.assertTrue(context["visual_identity_evidence_valid"])

    def test_v2_context_allows_one_authorized_layout_surplus_with_journal_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = "<article><p>Corpo editorial.</p>" + "".join(
                f'<img src="https://cdn.test/inline-{index}.webp" />'
                for index in range(1, 4)
            ) + "</article>"
            accepted = [
                {
                    "media_id": 100 + index,
                    "media_url": f"https://cdn.test/inline-{index}.webp",
                    "sha256": "a" * 64,
                    "phash": "b" * 16,
                    "visual_group_id": f"visual:{index}",
                    "visual_verification": {"decision": "DIFFERENT"},
                }
                for index in range(1, 5)
            ]
            state = {
                "media": {
                    "inline": {"required": 3, "accepted": accepted},
                    "featured": {"status": "valid", "media_id": 7, "media_url": "https://cdn.test/featured.webp"},
                    "layout_policy": {
                        "schema_version": 2,
                        "policy": "normal-article-layout-v2",
                        "layout": "normal_article",
                        "target_ideal": 4,
                        "target_effective": 3,
                        "reduction_reason": "layout_limited",
                    },
                }
            }
            from unicornio_editor.pipeline_v2.media_layout import _layout_sha256

            state["media"]["layout_policy"]["layout_sha256"] = _layout_sha256(
                state["media"]["layout_policy"]
            )
            post = {
                "id": 42,
                "status": "pending",
                "featured_media": 7,
                "content": {"raw": body},
            }
            journal_dir = root / "work" / "v2-journal"
            journal_dir.mkdir(parents=True)
            journal = {
                "status": "committed",
                "readback": True,
                "media_application_status": "confirmed",
                "html_readback_status": "confirmed",
                "candidate_hash": hashlib.sha256(body.encode()).hexdigest(),
                "apply_media_records": [
                    {
                        "role": "inline",
                        "media_id": 100 + index,
                        "readback_status": "confirmed",
                        "destination": "applied_readback_confirmed",
                    }
                    for index in range(1, 4)
                ],
                "media_records": [
                    {
                        "role": "inline",
                        "media_id": 104,
                        "destination": "accepted_working_not_applied",
                    }
                ],
            }
            (journal_dir / "42.json").write_text(json.dumps(journal), encoding="utf-8")

            context = _publish_v2_media_context(state, post, root=root)

            self.assertIsNotNone(context)
            self.assertTrue(context["visual_identity_evidence_valid"])
            self.assertEqual(context["applied_inline_ids"], [101, 102, 103])
            self.assertEqual(context["accepted_working_not_applied_ids"], [104])
            self.assertTrue(context["media_application_contract"]["journal_readback_proven"])

    def test_v2_context_rejects_surplus_without_structural_and_journal_proof(self):
        accepted = [
            {"media_id": index, "media_url": f"https://cdn.test/{index}.webp"}
            for index in range(1, 5)
        ]
        state = {
            "media": {
                "inline": {"required": 3, "accepted": accepted},
                "featured": {"status": "valid", "media_id": 7},
            }
        }
        post = {
            "id": 42,
            "featured_media": 7,
            "content": {"raw": '<img src="https://cdn.test/1.webp" />'},
        }
        context = _publish_v2_media_context(state, post)
        self.assertIsNotNone(context)
        self.assertFalse(context["visual_identity_evidence_valid"])

        state = {
            "media": {
                "inline": {"required": 2, "accepted": [], "accepted_count": 0, "missing": 2},
                "featured": {"status": "valid", "media_id": 7, "media_url": "https://media.example/featured.webp"},
                "search": {"completed": True, "exhausted": True, "completion_reason": "EXHAUSTED"},
                "enrichment_round": 1,
                "waiver": {"applied": True, "reason": "enrichment_search_exhausted"},
            }
        }
        post = {
            "status": "pending",
            "featured_media": 7,
            "content": {"raw": "<p>Texto sem imagens inline.</p>"},
        }

        context = _publish_v2_media_context(state, post)

        self.assertIsNotNone(context)
        self.assertTrue(context["visual_identity_evidence_valid"])
        self.assertEqual(context["waiver"]["reason"], "enrichment_search_exhausted")

    def test_publish_v2_featured_read_failure_retries_without_blocking_or_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            apply_editorial(client, self.config(False), root, 42, editorial_payload())
            self._attach_v2_ready_media(client)
            updates_before_publish = list(client.updated)
            client.get_media = lambda media_id: (_ for _ in ()).throw(TimeoutError("temporary timeout"))
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True,
            )

            outcome = publish_post(client, config, root, 42)

            self.assertEqual(outcome["status"], "retry")
            self.assertFalse(outcome["wordpress_changed"])
            self.assertEqual(len(client.updated), len(updates_before_publish) + 1)
            self.assertNotEqual(client.post["meta"].get("_hermes_state"), "blocked")
            reopened = WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"]))
            self.assertIs(reopened.state, LifecycleState.PENDING)
            self.assertIs(reopened.blocker, BlockerCode.PROVIDER_ERROR)
            self.assertFalse((root / "backups/42/editorial.blocked.json").exists())

    def test_publish_ready_from_previous_policy_revalidates_before_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            apply_editorial(client, self.config(False), root, 42, editorial_payload())
            config = Config(
                "wordpress", "http://wp.test", "/wp-json/wp/v2",
                dry_run=False, publish_enabled=True, policy_version=4,
            )
            outcome = publish_post(client, config, root, 42)
            self.assertEqual(outcome["status"], "published")
            self.assertEqual(outcome["integrity"], "revalidated")

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

    def test_retry_v2_awaiting_human_moves_to_pending_and_verifies_both_states(self):
        post = self.post()
        post["status"] = "awaiting_human"
        previous = WorkState(
            state=LifecycleState.HUMAN_REQUIRED,
            phase=Phase.MEDIA,
            blocker=BlockerCode.INLINE_MISSING,
            retry=replace(WorkState().retry, attempts=3, no_progress=2),
        )
        post["meta"] = {
            "_hermes_state": "awaiting_human",
            "_hermes_attempts": "3",
            "_hermes_work_state": json.dumps(previous.to_dict()),
        }

        class StatusClient(FakeClient):
            def move_to_status(self, post_id, status):
                self.post["status"] = status
                self.updated.append((post_id, {"status": status}))
                return {"id": post_id, "status": status}

        client = StatusClient(post)
        with tempfile.TemporaryDirectory() as directory:
            result = retry_post(client, self.config(False), Path(directory), 42)

        saved = WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"]))
        self.assertTrue(result["readback"])
        self.assertEqual(client.post["status"], "pending")
        self.assertIs(saved.state, LifecycleState.PENDING)
        self.assertIs(saved.phase, Phase.MEDIA)
        self.assertIs(saved.blocker, BlockerCode.INLINE_MISSING)
        self.assertEqual(saved.retry.attempts, 0)
        self.assertEqual(saved.retry.no_progress, 0)
        self.assertEqual(client.post["meta"]["_hermes_state"], "blocked")

    def test_discard_projects_terminal_skipped_into_v2_state(self):
        post = self.post()
        ready = WorkState(
            state=LifecycleState.READY,
            phase=Phase.VALIDATE,
            relevance_approved=True,
        )
        post["meta"] = {
            "_hermes_state": "ready",
            "_hermes_work_state": json.dumps(ready.to_dict()),
        }
        client = FakeClient(post)
        with tempfile.TemporaryDirectory() as directory:
            result = discard_post(client, self.config(False), Path(directory), 42, "decisão humana")
        saved = WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"]))
        self.assertEqual(result["state"], "skipped")
        self.assertIs(saved.state, LifecycleState.SKIPPED)
        self.assertEqual(client.post["meta"]["_hermes_state"], "skipped")

    def test_discard_post_marks_skipped_and_leaves_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            result = discard_post(client, self.config(False), root, 42, reason="sem imagens reais")
            self.assertEqual(result["status"], "discarded")
            self.assertEqual(result["state"], "skipped")
            self.assertEqual(client.post["meta"]["_hermes_state"], "skipped")
            self.assertTrue((root / "backups/42/uncertain.json").is_file())
            queue = build_queue_report(client, root)
            self.assertEqual(queue["uncertain"], 0)
            self.assertEqual(queue["awaiting_human"], 0)
            self.assertNotIn(42, queue["eligible_rework_ids"])
            self.assertIn(42, queue["skipped_ids"])

    def test_discard_from_uncertain_is_skipped_not_awaiting_human(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            post = self.post()
            post["meta"] = {"_hermes_state": "uncertain", "_hermes_attempts": "1"}
            client = FakeClient(post)
            result = discard_post(client, self.config(False), root, 42, reason="fora do escopo")
            self.assertEqual(result["state"], "skipped")
            self.assertEqual(client.post["meta"]["_hermes_state"], "skipped")
            queue = build_queue_report(client, root)
            self.assertNotIn(42, queue["uncertain_ids"])
            self.assertNotIn(42, queue["awaiting_human_ids"])
            self.assertNotIn(42, queue["eligible_rework_ids"])

    def test_discard_from_awaiting_human_returns_to_pending_and_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            post = self.post()
            post["status"] = "awaiting_human"
            post["meta"] = {"_hermes_state": "awaiting_human", "_hermes_attempts": "2"}
            client = FakeClient(post)
            result = discard_post(client, self.config(False), root, 42, reason="decisao humana definitiva")
            self.assertEqual(result["state"], "skipped")
            self.assertEqual(client.post["meta"]["_hermes_state"], "skipped")
            self.assertIn((42, {"status": "pending"}), client.updated)
            queue = build_queue_report(client, root)
            self.assertNotIn(42, queue["awaiting_human_ids"])
            self.assertNotIn(42, queue["uncertain_ids"])
            self.assertNotIn(42, queue["eligible_rework_ids"])

    def test_mark_uncertain_keeps_uncertain_state_and_retry_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            result = mark_uncertain(client, self.config(False), root, 42, reason="sem evidencia suficiente")
            self.assertEqual(result["status"], "uncertain")
            self.assertEqual(result["state"], "uncertain")
            self.assertEqual(client.post["meta"]["_hermes_state"], "uncertain")
            queue = build_queue_report(client, root)
            self.assertIn(42, queue["uncertain_ids"])
            self.assertNotIn(42, queue["skipped_ids"])

    def test_mark_uncertain_rejects_after_relevance_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backups/42").mkdir(parents=True)
            (root / "backups/42/editorial.latest.json").write_text(
                json.dumps({"site_relevance": {"decision": "process"}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(WorkflowError, "uncertain_not_allowed_after_relevance_process"):
                mark_uncertain(
                    FakeClient(self.post()), self.config(False), root, 42,
                    reason="sem imagem encontrada",
                )

    def test_mark_uncertain_rejects_after_relevance_process_in_batch_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "work/batches/batch-1/editorial.output.json"
            output.parent.mkdir(parents=True)
            output.write_text(json.dumps({
                "results": [{
                    "post_id": 42,
                    "status": "ok",
                    "editorial": {"site_relevance": {"decision": "process"}},
                }],
            }), encoding="utf-8")
            with self.assertRaisesRegex(WorkflowError, "uncertain_not_allowed_after_relevance_process"):
                mark_uncertain(
                    FakeClient(self.post()), self.config(False), root, 42,
                    reason="sem imagem encontrada",
                )

    def test_mark_uncertain_rejects_awaiting_human_without_state_split(self):
        with tempfile.TemporaryDirectory() as directory:
            post = self.post()
            post["status"] = "awaiting_human"
            post["meta"] = {"_hermes_state": "awaiting_human", "_hermes_attempts": "2"}
            with self.assertRaises(WorkflowError):
                mark_uncertain(FakeClient(post), self.config(False), Path(directory), 42, reason="duvida")

    def test_discard_reports_failure_when_awaiting_status_cannot_return_pending(self):
        class FailingMoveClient(FakeClient):
            def move_to_status(self, post_id, status):
                raise WordPressError("HTTP 503 ao mover status")

        with tempfile.TemporaryDirectory() as directory:
            post = self.post()
            post["status"] = "awaiting_human"
            post["meta"] = {"_hermes_state": "awaiting_human", "_hermes_attempts": "2"}
            root = Path(directory)
            with self.assertRaises(WorkflowError):
                discard_post(FailingMoveClient(post), self.config(False), root, 42, reason="definitivo")
            self.assertIn("discard_status_move_failed", (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8"))


    def test_discard_fails_closed_when_skipped_state_does_not_persist(self):
        class FailingClient(FakeClient):
            def update_post(self, post_id, payload):
                raise WordPressError("HTTP 500 ao persistir estado")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(WorkflowError):
                discard_post(
                    FailingClient(self.post()), self.config(False), root, 42,
                    reason="fora do escopo",
                )
            telemetry = (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
            self.assertIn("state_persist_failed", telemetry)

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
            "unicornio_editor.media.visual_identity.verify_candidate_identity",
            return_value=_verified_media_identity(),
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
            self.assertEqual(report["status"], "partial")
            summary = read_telemetry_summary(root)
            self.assertEqual(summary["by_event"].get("partial_started"), 1)
            self.assertEqual(summary["by_event"].get("partial_progress"), 1)


    def test_discard_aceita_awaiting_human(self):
        """P1.1: o discard humano funciona exatamente onde o pipeline para.

        Antes so `pending` era aceito, entao o operador precisava fazer
        retry->pending->discard justamente no post que pediu intervencao.
        """
        with tempfile.TemporaryDirectory() as directory:
            post = self.post()
            post["status"] = "awaiting_human"
            client = FakeClient(post)
            report = discard_post(
                client, self.config(False), Path(directory), 42, reason="fora da pauta"
            )
            self.assertEqual(report["status"], "discarded")
            self.assertEqual(report["state"], "skipped")
            # P1.2: discard NAO altera conteudo — so a meta de estado e gravada
            self.assertFalse(report["baseline_enriched"])
            self.assertTrue(all("content" not in payload for _pid, payload in client.updated))

    def test_discard_rejeita_status_invalido(self):
        """Status que nao pede intervencao humana continua recusado."""
        with tempfile.TemporaryDirectory() as directory:
            post = self.post()
            post["status"] = "publish"
            client = FakeClient(post)
            with self.assertRaises(WorkflowError):
                discard_post(client, self.config(False), Path(directory), 42)

    def test_write_state_markers_reporta_falha(self):
        """P1.3: falha ao persistir _hermes_state deixa de ser silenciosa.

        Sem telemetria, o post ficava sem estado no WP (reaparecia na fila,
        gerava retry e custo de LLM de novo) sem nenhum sinal operacional.
        """
        from unicornio_editor.workflow import _write_state_markers
        from unicornio_editor.observability import read_telemetry_summary

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(self.post())
            with mock.patch.object(client, "update_post", side_effect=RuntimeError("HTTP 500")):
                ok = _write_state_markers(
                    client, self.config(False), 42, "ready", root=root, ready_hash="abc"
                )
            self.assertFalse(ok)
            summary = read_telemetry_summary(root)
            self.assertEqual(summary["by_event"].get("state_persist_failed"), 1)

    def test_discard_de_awaiting_human_volta_status_para_pending(self):
        """Fase 16: o post descartado sai da fila HUMANA.

        Sem devolver o status para pending, um post descartado continuaria
        aparecendo no filtro "Awaiting Human" como se ainda esperasse decisao.
        """
        with tempfile.TemporaryDirectory() as directory:
            post = self.post()
            post["status"] = "awaiting_human"
            client = FakeClient(post)
            report = discard_post(
                client, self.config(False), Path(directory), 42, reason="fora da pauta"
            )
            self.assertEqual(report["status"], "discarded")
            self.assertTrue(
                any(payload.get("status") == "pending" for _pid, payload in client.updated),
                "o status WP deveria voltar para pending",
            )


    def test_migrate_legacy_state_dry_run_nao_escreve(self):
        """P3.7: a migração é explícita — sem --apply não grava nada."""
        from unicornio_editor.workflow import migrate_legacy_state

        with tempfile.TemporaryDirectory() as directory:
            legado = self.post()
            legado["status"] = "publish"
            legado.pop("meta", None)
            client = FakeClient(legado)
            client.list_pending = lambda **kw: [legado] if kw.get("status") == "publish" else []
            report = migrate_legacy_state(client, self.config(False), Path(directory))
            self.assertFalse(report["applied"])
            self.assertEqual(report["legacy_found"], 1)
            self.assertEqual(report["items"][0]["state"], "published")
            # dry-run: nada foi escrito no WordPress
            self.assertTrue(all("_hermes_state" not in (p or {}) for _pid, p in client.updated))

    def test_migrate_legacy_state_apply_grava_so_quem_nao_tem_estado(self):
        from unicornio_editor.workflow import migrate_legacy_state

        with tempfile.TemporaryDirectory() as directory:
            legado = self.post()
            legado["status"] = "publish"
            legado.pop("meta", None)
            com_estado = self.post()
            com_estado["id"] = 43
            com_estado["status"] = "publish"
            com_estado["meta"] = {"_hermes_state": "published"}
            client = FakeClient(legado)
            client.list_pending = lambda **kw: (
                [legado, com_estado] if kw.get("status") == "publish" else []
            )
            report = migrate_legacy_state(client, self.config(False), Path(directory), apply=True)
            self.assertTrue(report["applied"])
            self.assertEqual(report["legacy_found"], 1)  # o que já tinha estado ficou fora
            gravados = [
                pid for pid, payload in client.updated
                if "_hermes_state" in ((payload or {}).get("meta") or {})
            ]
            self.assertEqual(gravados, [42])


    def test_migrate_awaiting_human_legado_vira_awaiting_human(self):
        """Acceptance 11: `awaiting_human` legado NÃO pode virar `new`.

        Mapear para NEW criava uma divergência que o próprio reconcile acusaria
        (status WP diz espera humana, estado diz fila de automação).
        """
        from unicornio_editor.workflow import migrate_legacy_state

        with tempfile.TemporaryDirectory() as directory:
            legado = self.post()
            legado["status"] = "awaiting_human"
            legado.pop("meta", None)
            client = FakeClient(legado)
            client.list_pending = lambda **kw: (
                [legado] if kw.get("status") == "awaiting_human" else []
            )
            report = migrate_legacy_state(client, self.config(False), Path(directory), apply=True)
            self.assertTrue(report["applied"])
            self.assertEqual(report["applied_count"], 1)
            gravados = [
                payload["meta"].get("_hermes_state")
                for _pid, payload in client.updated
                if isinstance((payload or {}).get("meta"), dict)
            ]
            self.assertEqual(gravados, ["awaiting_human"])
            self.assertTrue(report["complete"])

    def test_migrate_reporta_falha_de_escrita(self):
        """Acceptance 12: falha ao gravar não pode aparecer como sucesso."""
        from unicornio_editor.workflow import migrate_legacy_state

        with tempfile.TemporaryDirectory() as directory:
            legado = self.post()
            legado["status"] = "publish"
            legado.pop("meta", None)
            client = FakeClient(legado)
            client.list_pending = lambda **kw: [legado] if kw.get("status") == "publish" else []
            with mock.patch.object(client, "update_post", side_effect=RuntimeError("HTTP 500")):
                report = migrate_legacy_state(client, self.config(False), Path(directory), apply=True)
            self.assertEqual(report["failed_count"], 1)
            self.assertEqual(report["applied_count"], 0)
            self.assertFalse(report["complete"])


    def test_apply_rejeita_imagem_de_outro_item_do_listicle(self):
        """Acceptance 3: alt/caption do agente NÃO convencem o apply.

        media_plan do item "Bleach" apontando uma imagem do Naruto COM
        alt/credit/search_query escritos "Bleach". Antes o apply consultava
        esses campos (o agente provava a si mesmo); agora só a evidência de
        origem + subject do item contam — e o item é rejeitado.
        """
        payload = editorial_payload()
        payload["cleaned_html"] = "<h2>1. Bleach</h2><p>Texto sobre o anime.</p>"
        item = self.media_item(paragraph_index=1)
        item.update({
            "direct_image_url": "https://cdn.x/naruto-keyart.jpg",
            "source_page_url": "https://site.com/naruto/",
            "subject": "Bleach",
            "alt_text": "Bleach anime",
            "credit_text": "Crédito da imagem: Bleach",
            "search_query": "Bleach anime",
        })
        payload["media_plan"] = [item]
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            # A rede não importa: o teste é sobre a RELEVÂNCIA, e um reject tem
            # de acontecer sem depender de download.
            with mock.patch("unicornio_editor.workflow.download_image") as baixar:
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        resultado = report["media_plan_results"][0]
        self.assertIn(resultado["status"], ("rejected", "error"))
        self.assertIn("relacao com o conteudo", str(resultado.get("detail") or ""))
        # e o download nem foi tentado para uma imagem sem relação
        self.assertFalse(baixar.called)

    def test_apply_aceita_key_art_do_proprio_item(self):
        """Contraprova: a imagem do PRÓPRIO subject continua passando."""
        payload = editorial_payload()
        payload["cleaned_html"] = "<h2>1. Bleach</h2><p>Texto sobre o anime.</p>"
        item = self.media_item(paragraph_index=1)
        item.update({
            "direct_image_url": "https://cdn.x/bleach-thousand-year-blood-war-keyart.jpg",
            "source_page_url": "https://site.com/bleach/",
            "subject": "Bleach",
        })
        payload["media_plan"] = [item]
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            with mock.patch("unicornio_editor.workflow.download_image") as baixar:
                baixar.side_effect = RuntimeError("parou aqui de proposito")
                apply_editorial(client, self.config(False), Path(directory), 42, payload)
        # passou da relevância (o download só falhou DEPOIS da validação)
        baixar.assert_called()


    def test_migrate_percorre_mais_de_uma_pagina(self):
        """Acceptance 10: 250 legados não podem parar em 100."""
        from unicornio_editor.workflow import migrate_legacy_state

        with tempfile.TemporaryDirectory() as directory:
            paginas = {1: [], 2: [], 3: []}
            for i in range(1, 251):
                pagina = (i - 1) // 100 + 1
                legado = self.post()
                legado["id"] = 1000 + i
                legado["status"] = "publish"
                legado.pop("meta", None)
                paginas[pagina].append(legado)

            client = FakeClient(self.post())
            client.list_pending = lambda **kw: (
                paginas.get(int(kw.get("page", 1)), []) if kw.get("status") == "publish" else []
            )
            report = migrate_legacy_state(client, self.config(False), Path(directory))
            self.assertEqual(report["legacy_found"], 250)
            self.assertEqual(report["scanned"], 250)


def test_final_phash_matches_accepted_baseline():
    assert _is_final_phash_duplicate("ccc04d9fb063ed1c", ["ccc04d9fb063ed1c"])


def test_final_phash_different_frame_is_not_duplicate():
    assert not _is_final_phash_duplicate("0000000000000000", ["ccc04d9fb063ed1c"])


def test_execute_media_plan_rejects_final_duplicate_before_upload(tmp_path):
    item = {
        "paragraph_index": 0,
        "source_page_url": "https://source.example/wolverine",
        "direct_image_url": "https://source.example/wolverine.jpg",
        "author": "Autor",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-10-07T00:00:00Z",
        "credit_text": "Crédito da imagem: Autor.",
        "alt_text": "Wolverine",
        "is_featured": False,
    }
    editorial = {
        "seo": {"title": "Wolverine videogame", "focus_keyword": "Wolverine"},
        "cleaned_html": "<p>Wolverine videogame.</p>",
        "media_plan": [item],
    }
    client = object()
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
    with _mock_source_rights(), \
         mock.patch("unicornio_editor.workflow._media_item_rejection", return_value=None), \
         mock.patch("unicornio_editor.workflow.download_image", return_value=tmp_path / "source.jpg"), \
         mock.patch("unicornio_editor.workflow.convert_to_webp", return_value=tmp_path / "inline.webp"), \
         mock.patch("unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "verified")), \
         mock.patch("unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)), \
         mock.patch("unicornio_editor.workflow.image_has_transparency", return_value=False), \
         mock.patch("unicornio_editor.workflow.image_is_mostly_flat", return_value=False), \
         mock.patch("unicornio_editor.media.visual_hash.phash_from_path", return_value="ccc04d9fb063ed1c"), \
         mock.patch("unicornio_editor.workflow.upload_image") as upload:
        results, _, _ = _execute_media_plan(
            editorial,
            config,
            client,
            tmp_path,
            preflight={"rejected": [], "featured_vision": []},
            post_id=115088,
            previous_inline_phashes=("ccc04d9fb063ed1c",),
        )
    upload.assert_not_called()
    assert results == [{"paragraph_index": 0, "status": "rejected", "detail": "duplicate_existing_frame"}]


def test_execute_media_plan_preserves_rejected_preflight_vision_without_upload(tmp_path):
    editorial = {"media_plan": [
        {"paragraph_index": 0, "is_featured": True, "candidate_id": "candidate-a"},
        {"paragraph_index": 1, "is_featured": True, "candidate_id": "candidate-b"},
    ]}
    vision = {
        "status": "UNRELATED", "confidence": 0.80, "visual_type": "photograph",
        "verdict": "reject", "image_sha256": "b" * 64, "image_role": "featured",
            "policy_version": "featured-editorial-v3-multidimensional", "decision_source": "vision_provider",
        "model_identity": "https://vision.test/vision-model", "context_key": "game",
        "subject": "Xbox Live",
    }
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
    with mock.patch("unicornio_editor.workflow.upload_image") as upload:
        results, _, _ = _execute_media_plan(
            editorial, config, object(), tmp_path,
            preflight={
                "rejected": [
                    {"index": 0, "reason": "source mismatch"},
                    {"index": 1, "reason": "UNRELATED 0.80 photograph"},
                ],
                "featured_vision": [{
                    "index": 1, "status": "rejected", "subject": "Xbox Live",
                    "category": "game", "image_sha256": "b" * 64,
                    "decision": {
                        "status": "UNRELATED", "confidence": 0.80,
                        "visual_type": "photograph", "verdict": "reject",
                    },
                    "vision_evidence": vision,
                }],
            },
            post_id=115286,
        )
    upload.assert_not_called()
    assert results[0]["status"] == "rejected"
    assert "vision_evidence" not in results[0]
    assert results[1]["vision_evidence"] == vision


def test_inline_vision_evidence_is_subject_model_policy_and_byte_bound(tmp_path):
    from unicornio_editor.media.vision_cache import vision_model_identity
    from unicornio_editor.media.vision_policy import featured_vision_context_key

    config = Config(
        "wordpress", "http://wp.test", "/wp-json/wp/v2",
        vision_enabled=True, vision_base_url="https://vision.test/v1", vision_model="model-a",
    )
    editorial = {
        "editorial_type": "game",
        "seo": {"title": "Halo", "focus_keyword": "Halo"},
        "cleaned_html": "<p>Halo</p>",
    }
    image_sha = "c" * 64
    item = {
        "subject": "Halo", "direct_image_url": "https://cdn.test/generic-image.webp",
        "source_page_url": "https://news.test/article", "is_featured": False,
        "evidence": {
            "vision_policy_version": "featured-editorial-v4-context-scoped",
            "vision_image_sha256": image_sha,
            "vision_decision": {
                "status": "MATCH", "confidence": 0.91, "visual_type": "key_art",
                "verdict": "accept", "image_sha256": image_sha, "image_role": "inline",
                "policy_version": "featured-editorial-v4-context-scoped",
                "model_identity": vision_model_identity(config.vision_base_url, config.vision_model),
                "context_key": featured_vision_context_key(editorial), "subject": "Halo",
            },
        },
    }
    assert _vision_evidence_matches_item(item, editorial, config)
    assert _vision_evidence_matches_item(item, editorial, config, image_sha256=image_sha)
    assert not _vision_evidence_matches_item(item, editorial, config, image_sha256="d" * 64)
    # Missing textual proof is acceptable only because the inline decision is
    # structured; a URL-only cache result no longer satisfies this gate.
    assert _media_item_rejection(item, {"Halo"}, object(), {}, editorial, root=tmp_path, config=config) is None
    item["evidence"]["vision_decision"]["model_identity"] = "https://vision.test/model-b"
    assert not _vision_evidence_matches_item(item, editorial, config)


def test_featured_preflight_reuse_requires_complete_context_identity():
    from unicornio_editor.media.vision_cache import vision_model_identity
    from unicornio_editor.media.vision_policy import (
        VISION_POLICY_VERSION, featured_vision_context_key, featured_vision_subject,
    )
    from unicornio_editor.workflow import _featured_preflight_evidence_matches

    config = Config(
        "wordpress", "http://wp.test", "/wp-json/wp/v2",
        vision_base_url="https://vision.test/v1", vision_model="model-a",
    )
    editorial = {
        "title": "Xbox Live atualiza salvaguardas",
        "editorial_type": "game",
        "seo": {"focus_keyword": "Xbox Live"},
    }
    evidence = {
        "status": "MATCH", "verdict": "accept", "image_sha256": "a" * 64,
        "image_role": "featured", "policy_version": VISION_POLICY_VERSION,
        "model_identity": vision_model_identity(config.vision_base_url, config.vision_model),
        "context_key": featured_vision_context_key(editorial),
        "subject": featured_vision_subject(editorial),
    }
    row = {"vision_evidence": evidence}

    assert _featured_preflight_evidence_matches(
        row, editorial, config, image_sha256="a" * 64,
    )
    assert not _featured_preflight_evidence_matches(
        row, {**editorial, "title": "Xbox Live launches new features"},
        config, image_sha256="a" * 64,
    )
    assert not _featured_preflight_evidence_matches(
        row, editorial, config, image_sha256="b" * 64,
    )
    for field, value in (
        ("policy_version", "obsolete-policy"),
        ("model_identity", "https://vision.test/model-b"),
        ("image_role", "inline"),
        ("verdict", "reject"),
    ):
        mismatched = {"vision_evidence": {**evidence, field: value}, "status": "passed"}
        assert not _featured_preflight_evidence_matches(
            mismatched, editorial, config, image_sha256="a" * 64,
        ), field


def test_execute_media_plan_uses_wordpress_title_context_for_inline_vision(tmp_path):
    from unicornio_editor.media.vision_cache import vision_model_identity
    from unicornio_editor.media.vision_policy import VISION_POLICY_VERSION, featured_vision_context_key

    config = Config(
        "wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=True,
        vision_enabled=True, vision_base_url="https://vision.test/v1", vision_model="model-a",
    )
    editorial = {
        "seo": {"title": "Título SEO diferente", "focus_keyword": "Xbox Live"},
        "cleaned_html": "<p>A notícia trata das salvaguardas da plataforma.</p>",
        "media_plan": [],
    }
    post_title = "Procuradoria-Geral investiga salvaguardas do Xbox Live"
    vision_editorial = {**editorial, "title": post_title}
    image_sha = "c" * 64
    item = {
        "paragraph_index": 0,
        "subject": "Xbox Live",
        "direct_image_url": "https://cdn.test/generic-image.webp",
        "source_page_url": "https://news.test/article",
        "is_featured": False,
        "evidence": {
            "vision_policy_version": VISION_POLICY_VERSION,
            "vision_image_sha256": image_sha,
            "vision_decision": {
                "status": "MATCH", "confidence": 0.91, "visual_type": "key_art",
                "verdict": "accept", "image_sha256": image_sha, "image_role": "inline",
                "policy_version": VISION_POLICY_VERSION,
                "model_identity": vision_model_identity(config.vision_base_url, config.vision_model),
                "context_key": featured_vision_context_key(vision_editorial),
                "subject": "Xbox Live",
            },
        },
    }
    editorial["media_plan"] = [item]

    results, _, _ = _execute_media_plan(
        editorial, config, object(), tmp_path, post_title=post_title,
    )

    # Dry-run reports "blocked" after passing relevance; "rejected" would
    # mean APPLY reconstructed a different context and discarded the evidence.
    assert results == [{
        "paragraph_index": 0,
        "status": "blocked",
        "detail": "dry-run nao executa download/upload de midia",
    }]


def test_normal_article_image_gate_uses_shared_canonical_identity():
    editorial = {
        "title": "Procuradoria-Geral investiga Xbox Live após denúncias",
        "cleaned_html": "<p>Xbox Live atualiza salvaguardas.</p>",
        "seo": {"focus_keyword": "Xbox Live"},
    }
    assert _item_entities(
        {"subject": "Procuradoria-Geral"}, {"procuradoria-geral"}, editorial
    ) == {"xbox live"}


def test_listicle_image_gate_keeps_section_subject():
    editorial = {
        "cleaned_html": (
            "<h2>1. Bleach</h2><p>Bleach update.</p>"
            "<h2>2. Naruto</h2><p>Naruto update.</p>"
        ),
        "seo": {"focus_keyword": "Bleach"},
    }
    assert _item_entities(
        {"subject": "bleach", "paragraph_index": 0}, {"bleach", "naruto"}, editorial
    ) == {"bleach"}


def test_execute_media_plan_uploads_new_final_phash(tmp_path):
    item = {
        "paragraph_index": 0,
        "source_page_url": "https://source.example/wolverine",
        "direct_image_url": "https://source.example/wolverine-new.jpg",
        "author": "Autor",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-10-07T00:00:00Z",
        "credit_text": "Crédito da imagem: Autor.",
        "alt_text": "Wolverine novo",
        "is_featured": False,
        "engine": "duckduckgo",
        "candidate_id": "ddg-wolverine-1",
    }
    item["rights_evidence"] = {
        "provider": "wikimedia_commons_api",
        "image_url": item["direct_image_url"],
        "author": item["author"],
        "license": item["license"],
        "license_url": item["license_url"],
    }
    from unicornio_editor.media.license import attest_wikimedia_rights
    item["rights_verified"] = True
    item["source_rights_attestation"] = attest_wikimedia_rights(item)
    editorial = {"seo": {"title": "Wolverine videogame", "focus_keyword": "Wolverine"}, "cleaned_html": "<p>Wolverine videogame.</p>", "media_plan": [item]}
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
    with _mock_source_rights(), \
         mock.patch("unicornio_editor.workflow._media_item_rejection", return_value=None), \
         mock.patch("unicornio_editor.workflow.download_image", return_value=tmp_path / "source.jpg"), \
         mock.patch("unicornio_editor.workflow.convert_to_webp", return_value=tmp_path / "inline.webp"), \
         mock.patch("unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "verified")), \
         mock.patch("unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)), \
         mock.patch("unicornio_editor.workflow.image_has_transparency", return_value=False), \
         mock.patch("unicornio_editor.workflow.image_is_mostly_flat", return_value=False), \
         mock.patch("unicornio_editor.media.visual_hash.phash_from_path", return_value="1111111111111111"), \
         mock.patch("unicornio_editor.media.visual_identity.verify_candidate_identity", return_value=_verified_media_identity()), \
         mock.patch("unicornio_editor.workflow.upload_image", create=True) as upload:
        upload.return_value = {"id": 115094, "source_url": "https://wp.test/115094.webp"}
        results, _, _ = _execute_media_plan(editorial, config, object(), tmp_path, preflight={"rejected": [], "featured_vision": []}, post_id=115088, previous_inline_phashes=("ccc04d9fb063ed1c",))
    upload.assert_called_once()
    assert results[0]["media_id"] == 115094
    index = json.loads((tmp_path / "work" / "media_index.json").read_text())
    assert index["entries"][-1]["phash"] == "1111111111111111"
    funnel = [
        json.loads(line)
        for line in (tmp_path / "work" / "telemetry.jsonl").read_text().splitlines()
        if json.loads(line).get("event") == "media_funnel"
    ]
    upload_event = next(
        event for event in funnel
        if event.get("stage") == "upload" and event.get("status") == "passed"
    )
    assert upload_event["engine"] == "duckduckgo"
    assert upload_event["candidate_id"] == "ddg-wolverine-1"
    assert "direct_image_url" not in upload_event


def test_execute_media_plan_does_not_upload_when_identity_verification_fails(tmp_path):
    item = {
        "paragraph_index": 0,
        "source_page_url": "https://source.example/wolverine",
        "direct_image_url": "https://source.example/wolverine-new.jpg",
        "author": "Autor",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-10-07T00:00:00Z",
        "credit_text": "Crédito da imagem: Autor.",
        "alt_text": "Wolverine novo",
        "is_featured": False,
    }
    editorial = {
        "seo": {"title": "Wolverine videogame", "focus_keyword": "Wolverine"},
        "cleaned_html": "<p>Wolverine videogame.</p>",
        "media_plan": [item],
    }
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
    with _mock_source_rights(), \
         mock.patch("unicornio_editor.workflow._media_item_rejection", return_value=None), \
         mock.patch("unicornio_editor.workflow.download_image", return_value=tmp_path / "source.jpg"), \
         mock.patch("unicornio_editor.workflow.convert_to_webp", return_value=tmp_path / "inline.webp"), \
         mock.patch("unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "verified")), \
         mock.patch("unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)), \
         mock.patch("unicornio_editor.workflow.image_has_transparency", return_value=False), \
         mock.patch("unicornio_editor.workflow.image_is_mostly_flat", return_value=False), \
         mock.patch("unicornio_editor.media.visual_hash.phash_from_path", return_value="1111111111111111"), \
         mock.patch("unicornio_editor.media.visual_identity.verify_candidate_identity", side_effect=RuntimeError("fingerprint unavailable")), \
         mock.patch("unicornio_editor.workflow.upload_image") as upload:
        results, _, _ = _execute_media_plan(
            editorial, config, object(), tmp_path,
            preflight={"rejected": [], "featured_vision": []}, post_id=115088,
        )
    upload.assert_not_called()
    assert results[0]["status"] == "rejected"
    assert "visual_identity_unverified" in results[0]["detail"]


def test_execute_media_plan_revalidates_changed_featured_bytes_before_upload(tmp_path):
    import hashlib

    from unicornio_editor.media.visual_identity import VisualIdentity

    item = {
        "paragraph_index": 0,
        "source_page_url": "https://source.example/xbox-live",
        "direct_image_url": "https://cdn.example/xbox-live-keyart.webp",
        "author": "Publisher",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-10-07T00:00:00Z",
        "credit_text": "Crédito da imagem: publisher",
        "alt_text": "Xbox Live key art",
        "subject": "Xbox Live",
        "is_featured": True,
    }
    editorial = {
        "seo": {"title": "Notícia sobre Xbox Live", "focus_keyword": "Xbox Live"},
        "cleaned_html": "<p>Xbox Live notícias.</p>",
        "game_name": "Xbox Live",
        "media_plan": [item],
    }
    source_path = tmp_path / "source.img"
    source_path.write_bytes(b"source bytes")
    final_path = tmp_path / "final.webp"
    final_bytes = b"new normalized featured bytes"
    final_sha256 = hashlib.sha256(final_bytes).hexdigest()
    identity = VisualIdentity(final_sha256, "1111111111111111", "visual:" + "a" * 24)
    config = Config(
        "wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False,
        vision_enabled=True, vision_api_key="test-key", vision_base_url="https://vision.test/v1",
    )

    with _mock_source_rights(), \
         mock.patch("unicornio_editor.workflow._media_item_rejection", return_value=None), \
         mock.patch("unicornio_editor.workflow.download_image", return_value=source_path), \
         mock.patch("unicornio_editor.workflow.prepare_featured_webp", side_effect=lambda *_args: (final_path.write_bytes(final_bytes), final_path)[1]), \
         mock.patch("unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "verified")), \
         mock.patch("unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)), \
         mock.patch("unicornio_editor.workflow.image_has_transparency", return_value=False), \
         mock.patch("unicornio_editor.workflow.image_is_mostly_flat", return_value=False), \
         mock.patch("unicornio_editor.media.visual_hash.phash_from_path", return_value="1111111111111111"), \
         mock.patch("unicornio_editor.media.visual_identity.verify_candidate_identity", return_value=(identity, VisualIdentityDecision("INITIAL", True, reason="test"))), \
         mock.patch("unicornio_editor.workflow.prepare_vision_image_input_from_path", return_value="data:image/webp;base64,ZmFrZQ==") as prepare_vision, \
         mock.patch("unicornio_editor.workflow.verify_image_subject_decision", return_value=_vision_result(accepted=False, status="UNRELATED", confidence=0.94)) as vision, \
         mock.patch("unicornio_editor.workflow.upload_image") as upload:
        results, _, _ = _execute_media_plan(
            editorial, config, object(), tmp_path,
            preflight={"rejected": [], "featured_vision": [{
                "index": 0,
                "status": "passed",
                "image_sha256": "b" * 64,
                "decision": _vision_result(accepted=True),
            }]},
            post_id=115286,
        )

    prepare_vision.assert_called_once_with(final_path)
    vision.assert_called_once()
    upload.assert_not_called()
    assert results[0]["status"] == "rejected"
    assert "share card" in results[0]["detail"]


def test_execute_media_plan_reuses_featured_decision_when_final_sha_matches(tmp_path):
    import hashlib

    from unicornio_editor.media.visual_identity import VisualIdentity
    from unicornio_editor.media.vision_cache import vision_model_identity
    from unicornio_editor.media.vision_policy import (
        VISION_POLICY_VERSION, featured_vision_context_key, featured_vision_subject,
    )

    item = {
        "paragraph_index": 0,
        "source_page_url": "https://source.example/xbox-live",
        "direct_image_url": "https://cdn.example/xbox-live-keyart.webp",
        "author": "Publisher", "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-10-07T00:00:00Z",
        "credit_text": "Crédito da imagem: publisher",
        "alt_text": "Xbox Live key art", "subject": "Xbox Live", "is_featured": True,
    }
    editorial = {
        "seo": {"title": "Notícia sobre Xbox Live", "focus_keyword": "Xbox Live"},
        "cleaned_html": "<p>Xbox Live notícias.</p>", "game_name": "Xbox Live",
        "media_plan": [item],
    }
    source_path = tmp_path / "source.img"
    source_path.write_bytes(b"source bytes")
    final_path = tmp_path / "final.webp"
    final_bytes = b"same normalized featured bytes"
    final_path.write_bytes(final_bytes)
    final_sha256 = hashlib.sha256(final_bytes).hexdigest()
    identity = VisualIdentity(final_sha256, "1111111111111111", "visual:" + "a" * 24)
    config = Config(
        "wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False,
        vision_enabled=True, vision_api_key="test-key", vision_base_url="https://vision.test/v1",
    )
    approved = _vision_result(accepted=True)
    preflight_evidence = {
        **approved,
        "image_sha256": final_sha256,
        "image_role": "featured",
        "policy_version": VISION_POLICY_VERSION,
        "model_identity": vision_model_identity(config.vision_base_url, config.vision_model),
        "context_key": featured_vision_context_key(editorial),
        "subject": featured_vision_subject(editorial),
    }

    with _mock_source_rights(), \
         mock.patch("unicornio_editor.workflow._media_item_rejection", return_value=None), \
         mock.patch("unicornio_editor.workflow.download_image", return_value=source_path), \
         mock.patch("unicornio_editor.workflow.prepare_featured_webp", return_value=final_path), \
         mock.patch("unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "verified")), \
         mock.patch("unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)), \
         mock.patch("unicornio_editor.workflow.image_has_transparency", return_value=False), \
         mock.patch("unicornio_editor.workflow.image_is_mostly_flat", return_value=False), \
         mock.patch("unicornio_editor.media.visual_hash.phash_from_path", return_value="1111111111111111"), \
         mock.patch("unicornio_editor.media.visual_identity.verify_candidate_identity", return_value=(identity, VisualIdentityDecision("INITIAL", True, reason="test"))), \
         mock.patch("unicornio_editor.workflow.verify_image_subject_decision") as vision, \
         mock.patch("unicornio_editor.workflow.upload_image") as upload:
        upload.return_value = {"id": 115287, "source_url": "https://wp.test/115287.webp"}
        results, _, _ = _execute_media_plan(
            editorial, config, object(), tmp_path,
            preflight={"rejected": [], "featured_vision": [{
                "index": 0, "status": "passed", "image_sha256": final_sha256,
                "decision": approved,
                "vision_evidence": preflight_evidence,
            }]},
            post_id=115286,
        )

    vision.assert_not_called()
    upload.assert_called_once()
    assert results[0]["status"] == "accepted", results
    assert results[0]["sha256"] == final_sha256


def test_featured_preflight_to_apply_keeps_wordpress_context_and_evidence(tmp_path):
    import hashlib

    from unicornio_editor.media.vision_policy import featured_vision_context_key, featured_vision_subject

    post_title = "Procuradoria-Geral investiga salvaguardas do Xbox Live"
    editorial = {
        "seo": {"title": "Título SEO diferente", "focus_keyword": "Xbox Live"},
        "cleaned_html": "<p>A notícia trata das salvaguardas da plataforma.</p>",
        "media_plan": [{
            "candidate_id": "115286-featured-new",
            "paragraph_index": 0,
            "subject": "Xbox Live",
            "search_query": "Xbox Live key art",
            "direct_image_url": "https://cdn.test/xbox-live-keyart.webp",
            "source_page_url": "https://news.test/xbox-live",
            "is_featured": True,
            "alt_text": "Xbox Live key art",
            "credit_text": "Crédito da imagem: publisher",
            "author": "Publisher",
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "captured_at": "2026-10-15T00:00:00Z",
        }],
    }
    config = Config(
        "wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False,
        vision_enabled=True, vision_api_key="test-key",
        vision_base_url="https://vision.test/v1", vision_model="model-a",
    )
    source_path = tmp_path / "source.webp"
    final_path = tmp_path / "final.webp"
    final_bytes = b"simulated normalized featured bytes"
    source_path.write_bytes(final_bytes)
    final_path.write_bytes(final_bytes)
    image_sha256 = hashlib.sha256(final_bytes).hexdigest()
    identity = VisualIdentity(image_sha256, "1111111111111111", "visual:" + "a" * 24)
    accepted_decision = {**_vision_result(accepted=True), "role_fit": "unsuitable"}

    evaluated_subjects = []

    def approved_preflight(_item, vision_editorial, *_args, **_kwargs):
        assert vision_editorial["title"] == post_title
        evaluated_subjects.append(featured_vision_subject(vision_editorial))
        return {
            "status": "passed", "image_sha256": image_sha256,
            "decision": accepted_decision,
        }

    preflight_upload_evidence = []
    with _mock_source_rights(), mock.patch(
        "unicornio_editor.workflow._validate_featured_candidate_vision",
        side_effect=approved_preflight,
    ) as vision_preflight, \
         mock.patch("unicornio_editor.workflow.download_image", return_value=source_path), \
         mock.patch("unicornio_editor.workflow.verify_downloaded_against_source", return_value=(True, "verified")), \
         mock.patch("unicornio_editor.workflow.prepare_featured_webp", return_value=final_path), \
         mock.patch("unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)), \
         mock.patch("unicornio_editor.workflow.image_has_transparency", return_value=False), \
         mock.patch("unicornio_editor.workflow.image_is_mostly_flat", return_value=False), \
         mock.patch("unicornio_editor.media.visual_hash.phash_from_path", return_value="1111111111111111"), \
         mock.patch(
             "unicornio_editor.media.visual_identity.verify_candidate_identity",
             return_value=(identity, VisualIdentityDecision("INITIAL", True, reason="test")),
         ), \
         mock.patch("unicornio_editor.workflow.verify_image_subject_decision") as vision_apply, \
         mock.patch(
             "unicornio_editor.workflow.upload_image",
             return_value={"id": 115287, "source_url": "https://wp.test/115287.webp"},
         ) as upload:
        preflight = validate_media_plan(
            object(), editorial, config=config, root=tmp_path,
            post_title=post_title, post_id=115286,
        )
        assert preflight["valid"] == 1
        assert preflight["rejected"] == []
        vision_preflight.assert_called_once()

        results, _, _ = _execute_media_plan(
            editorial, config, object(), tmp_path,
            preflight=preflight, post_title=post_title, post_id=115286,
            on_accepted=preflight_upload_evidence.append,
        )

    assert results[0]["status"] == "accepted", results
    assert upload.call_count == 1
    vision_apply.assert_not_called()  # exact normalized SHA + context reused
    assert preflight_upload_evidence[0]["vision_evidence"]["image_sha256"] == image_sha256
    assert preflight_upload_evidence[0]["vision_evidence"]["context_key"] == featured_vision_context_key(
        {**editorial, "title": post_title}
    )
    assert preflight_upload_evidence[0]["vision_evidence"]["subject"] == featured_vision_subject(
        {**editorial, "title": post_title}
    )
    assert preflight_upload_evidence[0]["vision_evidence"]["subject"] == evaluated_subjects[0]
    assert preflight_upload_evidence[0]["vision_evidence"]["role_fit"] == "unsuitable"

    from unicornio_editor.media.registry import connect as connect_registry
    from unicornio_editor.media.registry import record_v2_candidate_decision
    from unicornio_editor.pipeline_v2.runtime import _attach_candidate_vision_evidence

    candidate_record = dict(editorial["media_plan"][0])
    _attach_candidate_vision_evidence(
        candidate_record, preflight_upload_evidence[0]["vision_evidence"]
    )
    record_v2_candidate_decision(
        tmp_path, post_id=115286, run_id="offline-featured-test",
        candidate=candidate_record, role="featured", disposition="accepted",
        reason_code="candidate_accepted",
    )
    with connect_registry(tmp_path) as db:
        registry_evidence = json.loads(db.execute(
            "SELECT evidence_reference FROM media_decisions WHERE post_id=?",
            (115286,),
        ).fetchone()[0])
    assert registry_evidence["vision_decision"]["subject"] == evaluated_subjects[0]
    assert registry_evidence["vision_decision"]["role_fit"] == "unsuitable"


def test_article_source_candidate_does_not_re_resolve_provenance(tmp_path):
    item = {
        "paragraph_index": 0,
        "source_page_url": "https://news.example/article",
        "direct_image_url": "https://cdn.other.example/frame.webp",
        "origin_type": "article_source",
        "provenance_verified": True,
        "author": "Fonte",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-10-07T00:00:00Z",
        "credit_text": "Crédito da imagem: fonte.example",
        "alt_text": "Wolverine",
        "is_featured": False,
    }
    editorial = {
        "seo": {"title": "Wolverine videogame", "focus_keyword": "Wolverine"},
        "cleaned_html": "<p>Wolverine videogame.</p>",
        "media_plan": [item],
    }
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
    with _mock_source_rights(), \
         mock.patch("unicornio_editor.workflow._media_item_rejection", return_value=None), \
         mock.patch("unicornio_editor.workflow.download_image", return_value=tmp_path / "source.jpg"), \
         mock.patch("unicornio_editor.workflow.convert_to_webp", return_value=tmp_path / "inline.webp"), \
         mock.patch("unicornio_editor.workflow.verify_downloaded_against_source") as verify, \
         mock.patch("unicornio_editor.workflow.image_dimensions", return_value=(1280, 720)), \
         mock.patch("unicornio_editor.workflow.image_has_transparency", return_value=False), \
         mock.patch("unicornio_editor.workflow.image_is_mostly_flat", return_value=False), \
         mock.patch("unicornio_editor.media.visual_hash.phash_from_path", return_value="2222222222222222"), \
         mock.patch("unicornio_editor.media.visual_identity.verify_candidate_identity", return_value=_verified_media_identity()), \
         mock.patch("unicornio_editor.workflow.upload_image", return_value={"id": 115095, "source_url": "https://wp.test/115095.webp"}):
        results, _, _ = _execute_media_plan(
            editorial,
            config,
            object(),
            tmp_path,
            preflight={"rejected": [], "featured_vision": []},
            post_id=115100,
        )
    verify.assert_not_called()
    assert results[0]["media_id"] == 115095



if __name__ == "__main__":
    unittest.main()
