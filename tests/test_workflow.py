import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unicornio_editor.config import Config
from unicornio_editor.workflow import apply_editorial, prepare_post, publish_post


def editorial_payload(decision="process"):
    return {
        "site_relevance": {
            "decision": decision,
            "confidence": 0.99,
            "reason": "Teste",
            "matched_topics": ["games"] if decision == "process" else [],
        },
        "cleaned_html": "<p>Texto revisado.</p>",
        "seo": {
            "title": "Título sobre videogame e lançamento importante",
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

    def get_media(self, media_id):
        return {
            "id": media_id,
            "source_url": "https://wp.test/uploads/featured.webp",
            "media_details": {"width": 1200, "height": 720},
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
            "content": {"raw": "<article><p>Original.</p></article>"},
            "meta": {"original_link": "https://source.example/news"},
        }

    def test_prepare_creates_snapshot_and_cleans_content(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            report = prepare_post(client, Path(directory), 42)
            self.assertEqual(report["post_id"], 42)
            self.assertEqual(report["cleaned_html"], "<p>Original.</p>")
            self.assertTrue(Path(report["backup"]).exists())
            self.assertEqual(client.updated, [])

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
            "source_page_url": "https://source.example/page",
            "direct_image_url": "https://source.example/image.jpg",
            "author": "Autor",
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "captured_at": "2026-08-20T12:00:00Z",
            "credit_text": "Crédito da imagem: Autor. Imagem do jogo. CC BY 4.0.",
            "alt_text": "Imagem do jogo",
            "is_featured": is_featured,
        }

    def test_apply_executes_media_plan_and_sets_featured(self):
        payload = editorial_payload()
        payload["cleaned_html"] = "<p>Um.</p><p>Dois.</p><p>Tres.</p><p>Quatro.</p><p>Cinco.</p>"
        payload["media_plan"] = [
            self.media_item(paragraph_index=1),
            self.media_item(paragraph_index=4, is_featured=True),
        ]
        with mock.patch("unicornio_editor.workflow.download_image", return_value=Path("/tmp/source.jpg")), mock.patch(
            "unicornio_editor.workflow.convert_to_webp", return_value=Path("/tmp/inline.webp")
        ), mock.patch(
            "unicornio_editor.workflow.prepare_featured_webp", return_value=Path("/tmp/featured.webp")
        ), mock.patch(
            "unicornio_editor.workflow.upload_image",
            side_effect=[
                {"id": 50, "source_url": "https://wp.test/50.webp"},
                {"id": 51, "source_url": "https://wp.test/51.webp"},
            ],
        ):
            with tempfile.TemporaryDirectory() as directory:
                client = FakeClient(self.post())
                report = apply_editorial(client, self.config(False), Path(directory), 42, payload)
        payload_sent = client.updated[0][1]
        self.assertEqual(payload_sent["featured_media"], 51)
        raw = payload_sent["content"]["raw"]
        self.assertIn("https://wp.test/50.webp", raw)
        self.assertIn("Crédito da imagem", raw)
        self.assertEqual(report["featured_media"], 51)
        self.assertEqual(len(report["media_plan_results"]), 2)
        self.assertTrue(report["media_plan_results"][1]["featured"])

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

    def test_apply_normalizes_existing_featured_to_1200x720(self):
        post = self.post()
        post["featured_media"] = 7
        media = {
            "id": 7,
            "source_url": "https://wp.test/uploads/old-featured.jpg",
            "media_details": {"width": 2560, "height": 1916},
            "alt_text": "Capa antiga",
            "title": {"raw": "Capa"},
            "caption": {"raw": "Crédito da imagem: Autor."},
        }

        class MediaClient(FakeClient):
            def get_media(self, media_id):
                return media

            def upload_media(self, path, *, filename, alt_text, title, caption=None):
                self.uploads = getattr(self, "uploads", 0) + 1
                return {"id": 88, "source_url": "https://wp.test/uploads/featured-1200x720.webp"}

        with mock.patch("unicornio_editor.workflow.download_image", return_value=Path("/tmp/old.jpg")), mock.patch(
            "unicornio_editor.workflow.prepare_featured_webp", return_value=Path("/tmp/new.webp")
        ):
            with tempfile.TemporaryDirectory() as directory:
                client = MediaClient(post)
                report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload())
        self.assertEqual(client.uploads, 1)
        self.assertEqual(client.updated[0][1]["featured_media"], 88)
        self.assertEqual(report["featured_media"], 88)

    def test_apply_keeps_compliant_featured_image(self):
        post = self.post()
        post["featured_media"] = 7
        media = {
            "id": 7,
            "source_url": "https://wp.test/uploads/featured.webp",
            "media_details": {"width": 1200, "height": 720},
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
                '<figure class="aligncenter"><img src="https://wp.test/1.webp" alt="a" /></figure>'
                "<p>Mais texto sobre videogame e jogos.</p>"
                '<figure class="aligncenter"><img src="https://wp.test/2.webp" alt="b" /></figure>'
                '<p>Fonte: <a href="https://source.example/news" rel="nofollow noopener">Source</a>.</p>'
                "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
            )
        }
        post["featured_media"] = 7
        return post

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


if __name__ == "__main__":
    unittest.main()
