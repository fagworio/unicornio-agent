import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unicornio_editor.checklist import run_pre_publish_checklist
from unicornio_editor.config import Config


def editorial_payload(**overrides):
    payload = {
        "site_relevance": {
            "decision": "process",
            "confidence": 0.99,
            "reason": "Notícia sobre videogame",
            "matched_topics": ["games"],
        },
        "cleaned_html": "<p>Texto revisado sobre o jogo.</p>",
        "seo": {
            "title": "Notícia sobre videogame e lançamento importante",
            "meta_description": "Uma descrição suficientemente longa sobre o conteúdo de videogame, seus detalhes, plataformas e contexto para o leitor entender a notícia.",
            "focus_keyword": "videogame",
        },
        "media_plan": [],
        "needs_trailer": False,
        "trailer_url": None,
        "game_name": None,
    }
    payload.update(overrides)
    return payload


class FakeClient:
    def get_media(self, media_id):
        # Simulates the post-fix normalize behavior: the re-uploaded featured
        # keeps provenance evidence in its filename/title/alt, so the
        # featured relevance gate can match the work from real evidence.
        return {
            "id": media_id,
            "source_url": "https://media.example/redfall-1280x720.webp",
            "title": {"rendered": "Redfall key art"},
            "alt_text": "Redfall key art",
            "media_details": {"width": 1280, "height": 720},
        }


def make_post(**overrides):
    post = {
        "id": 42,
        "status": "pending",
        "title": {"raw": "Notícia sobre videogame"},
        "content": {"raw": "<p>Original.</p>"},
        "meta": {},
        "featured_media": 7,
    }
    post.update(overrides)
    return post


class ChecklistTests(unittest.TestCase):
    def config(self):
        return Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=True)

    def _run_checklist(self, post=None, editorial=None, content=None, backup=True, client=None):
        with tempfile.TemporaryDirectory() as directory:
            backup_path = Path(directory) / "backups" / "42" / "snapshot.json"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if backup:
                backup_path.write_text("{}")
            return run_pre_publish_checklist(
                post=post or make_post(),
                editorial=editorial or editorial_payload(),
                content=content or "<p>Texto revisado sobre o jogo videogame.</p>",
                backup_path=backup_path if backup else None,
                config=self.config(),
                client=client or FakeClient(),
            )

    def statuses(self, result):
        return {item["name"]: item["status"] for item in result["items"]}

    def test_all_pass_when_every_rule_is_satisfied(self):
        content = (
            '<figure class="aligncenter"><img src="https://media.example/a.webp" width="1280" height="720" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            "<p>Texto revisado sobre o jogo videogame.</p>"
            '<figure class="aligncenter"><img src="https://media.example/b.webp" width="1280" height="720" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            "<p>Mais texto sobre videogame e o lançamento.</p>"
            '<figure class="aligncenter"><img src="https://media.example/c.webp" width="1280" height="720" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            "<p>Fechando o texto sobre videogame.</p>"
            '<figure class="aligncenter"><img src="https://media.example/d.webp" width="1280" height="720" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            "<p>Último parágrafo com videogame.</p>"
            "<hr /><h3>Confira mais novidades em nosso Portal de Notícias!</h3><hr />"
        )
        editorial = editorial_payload(
            game_name="Meu Jogo",
            **{"seo": {"title": "Redfall ganha data de lançamento", "meta_description": "x" * 130, "focus_keyword": "Redfall"}},
        )
        post = make_post(meta={"original_link": "https://source.example/noticia"})
        content = content + (
            '<em>Fonte: <a href="https://source.example/noticia" target="_blank" rel="nofollow noopener">Source</a>.</em>'
            '<iframe src="https://www.youtube-nocookie.com/embed/abcDEF12345" allowfullscreen></iframe>'
        )
        result = self._run_checklist(post=post, editorial=editorial, content=content)
        statuses = self.statuses(result)
        self.assertTrue(result["all_passed"], result["items"])
        self.assertEqual(result["failed"], 0)

    def test_fonte_fails_when_original_link_exists_without_source_block(self):
        post = make_post(meta={"original_link": "https://source.example/noticia"})
        result = self._run_checklist(post=post)
        self.assertEqual(self.statuses(result)["fonte_original_link"], "fail")
        self.assertFalse(result["all_passed"])

    def test_fonte_skips_when_no_original_link(self):
        result = self._run_checklist()
        self.assertEqual(self.statuses(result)["fonte_original_link"], "skip")

    def test_featured_image_fails_when_missing(self):
        result = self._run_checklist(post=make_post(featured_media=0))
        self.assertEqual(self.statuses(result)["imagem_destaque"], "fail")

    def test_body_images_fail_below_word_count_rule(self):
        # Short content requires 2 images; only 1 present.
        content = (
            '<figure class="aligncenter"><img src="https://media.example/a.webp" alt="Notícia sobre videogame" /></figure>'
            "<p>Texto revisado sobre o jogo videogame.</p>"
        )
        result = self._run_checklist(content=content)
        self.assertEqual(self.statuses(result)["imagens_no_corpo"], "fail")

    def test_body_images_fail_when_no_image_available(self):
        # The 2/4/6 minimum ALWAYS holds: an image-less post must not pass.
        result = self._run_checklist()
        self.assertEqual(self.statuses(result)["imagens_no_corpo"], "fail")
        self.assertEqual(self.statuses(result)["qualidade_texto"], "fail")

    def test_irrelevant_image_fails_relevance_gate(self):
        # A real bat is NOT a valid image for a videogame news post.
        content = (
            '<figure class="aligncenter"><img src="https://media.example/morcego.webp" alt="Morcego real em voo" />'
            "<figcaption>Crédito da imagem: Fotógrafo. Morcego real. CC0.</figcaption></figure>"
            "<p>Texto revisado sobre o jogo videogame.</p>"
            "<hr /><h3>Confira mais novidades em nosso Portal de Notícias!</h3><hr />"
        )
        result = self._run_checklist(content=content)
        self.assertEqual(self.statuses(result)["relevancia_imagens"], "fail")

    def test_duplicate_image_fails_duplicate_gate(self):
        # Reutilizar a mesma URL de imagem varias vezes no corpo e falha
        # editorial (o "mesma key art reaproveitada no post" da producao).
        content = (
            '<figure class="aligncenter"><img src="https://media.example/a.webp" width="1280" height="720" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            "<p>Texto revisado sobre o jogo videogame.</p>"
            '<figure class="aligncenter"><img src="https://media.example/a.webp" width="1280" height="720" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            "<p>Mais texto sobre videogame e o lançamento.</p>"
        )
        result = self._run_checklist(content=content)
        self.assertEqual(self.statuses(result)["imagens_duplicadas"], "fail")
        self.assertFalse(result["all_passed"])

    def test_distinct_images_pass_duplicate_gate(self):
        content = (
            '<figure class="aligncenter"><img src="https://media.example/a.webp" width="1280" height="720" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            "<p>Texto revisado sobre o jogo videogame.</p>"
            '<figure class="aligncenter"><img src="https://media.example/b.webp" width="1280" height="720" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
        )
        result = self._run_checklist(content=content)
        self.assertEqual(self.statuses(result)["imagens_duplicadas"], "pass")

    def test_relevant_image_passes_relevance_gate(self):
        content = (
            '<figure class="aligncenter"><img src="https://media.example/a.webp" alt="Cena importante do jogo" />'
            "<figcaption>Crédito da imagem: Autor. Cena importante do jogo. CC BY 4.0.</figcaption></figure>"
            "<p>Texto revisado sobre o jogo videogame.</p>"
            "<hr /><h3>Confira mais novidades em nosso Portal de Notícias!</h3><hr />"
        )
        result = self._run_checklist(content=content)
        self.assertEqual(self.statuses(result)["relevancia_imagens"], "pass")

    def test_webp_fails_for_inline_jpg(self):
        content = '<figure class="aligncenter"><img src="https://media.example/foto.jpg" alt="Notícia sobre videogame" /></figure><p>Texto videogame.</p>'
        result = self._run_checklist(content=content)
        self.assertEqual(self.statuses(result)["imagens_webp"], "fail")

    def test_webp_skips_when_no_images(self):
        result = self._run_checklist(post=make_post(featured_media=0))
        self.assertEqual(self.statuses(result)["imagens_webp"], "skip")

    def test_trailer_fails_when_game_name_without_embed(self):
        result = self._run_checklist(editorial=editorial_payload(game_name="Meu Jogo"))
        self.assertEqual(self.statuses(result)["trailer_youtube"], "fail")

    def test_trailer_skips_for_non_game_content(self):
        result = self._run_checklist()
        self.assertEqual(self.statuses(result)["trailer_youtube"], "skip")

    def test_cta_fails_when_missing(self):
        result = self._run_checklist(content="<p>Texto revisado sobre o jogo videogame.</p>")
        self.assertEqual(self.statuses(result)["cta_canonico"], "fail")

    def test_status_fails_when_post_not_pending(self):
        result = self._run_checklist(post=make_post(status="publish"))
        self.assertEqual(self.statuses(result)["status_pending"], "fail")

    def test_backup_fails_when_snapshot_missing(self):
        result = self._run_checklist(backup=False)
        self.assertEqual(self.statuses(result)["backup"], "fail")

    def test_schema_fails_on_invalid_editorial(self):
        editorial = editorial_payload()
        editorial["seo"] = {"title": "x" * 66, "meta_description": "curta", "focus_keyword": "jogo"}
        result = self._run_checklist(editorial=editorial)
        self.assertEqual(self.statuses(result)["schema_editorial"], "fail")

    def test_featured_relevance_validates_real_attachment_evidence(self):
        # The gate must validate the REAL featured attachment (url+title+alt,
        # source-only) — an attachment whose filename/title carry no entity of
        # the post fails even when the media_plan has a featured item with a
        # decorated source (the exact "Disney castle captioned as Kingdom
        # Hearts" case).
        class GenericFeaturedClient(FakeClient):
            def get_media(self, media_id):
                return {
                    "id": media_id,
                    "source_url": "https://media.example/featured-1280x720.webp",
                    "title": {"rendered": "Imagem de destaque"},
                    "alt_text": "Imagem de destaque",
                    "media_details": {"width": 1280, "height": 720},
                }

        editorial = editorial_payload(
            media_plan=[
                {
                    "paragraph_index": 0,
                    "source_page_url": "https://example.com/redfall-keyart",
                    "direct_image_url": "https://media.example/redfall.webp",
                    "author": "Autor",
                    "license": "CC BY 4.0",
                    "license_url": "https://creativecommons.org/licenses/by/4.0",
                    "captured_at": "2026-08-01",
                    "credit_text": "Crédito da imagem: Autor. Redfall. CC BY 4.0.",
                    "alt_text": "Redfall key art",
                    "is_featured": True,
                }
            ],
            game_name="Redfall",
        )
        post = make_post(
            meta={"original_link": "https://source.example/noticia"},
            content={"raw": "<p>Redfall.</p>"},
        )
        result = self._run_checklist(
            post=post, editorial=editorial, client=GenericFeaturedClient()
        )
        self.assertEqual(self.statuses(result)["destaque_relevancia"], "fail")

    def test_topic_gate_fails_when_no_overlap_with_site_topics(self):
        # matched_topics fora da lista do site -> qualidade_texto reprova.
        config = Config(
            "wordpress",
            "http://wp.test",
            "/wp-json/wp/v2",
            dry_run=True,
            site_topics=("games", "anime"),
        )
        content = (
            '<figure class="aligncenter"><img src="https://media.example/a.webp" alt="Cena importante do jogo" /></figure>'
            "<p>Texto revisado sobre o jogo videogame.</p>"
            '<figure class="aligncenter"><img src="https://media.example/b.webp" alt="Cena importante do jogo" /></figure>'
        )
        with tempfile.TemporaryDirectory() as directory:
            backup_path = Path(directory) / "backups" / "42" / "snapshot.json"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_text("{}")
            editorial = editorial_payload()
            editorial["site_relevance"]["matched_topics"] = ["economia"]
            result = run_pre_publish_checklist(
                post=make_post(),
                editorial=editorial,
                content=content,
                backup_path=backup_path,
                config=config,
                client=FakeClient(),
            )
        self.assertEqual(self.statuses(result)["qualidade_texto"], "fail")

    def test_topic_gate_passes_with_overlap(self):
        config = Config(
            "wordpress",
            "http://wp.test",
            "/wp-json/wp/v2",
            dry_run=True,
            site_topics=("games", "anime"),
        )
        content = (
            '<figure class="aligncenter"><img src="https://media.example/a.webp" alt="Cena importante do jogo" /></figure>'
            "<p>Texto revisado sobre o jogo videogame.</p>"
            '<figure class="aligncenter"><img src="https://media.example/b.webp" alt="Cena importante do jogo" /></figure>'
        )
        with tempfile.TemporaryDirectory() as directory:
            backup_path = Path(directory) / "backups" / "42" / "snapshot.json"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_text("{}")
            editorial = editorial_payload()
            editorial["site_relevance"]["matched_topics"] = ["games"]
            result = run_pre_publish_checklist(
                post=make_post(),
                editorial=editorial,
                content=content,
                backup_path=backup_path,
                config=config,
                client=FakeClient(),
            )
        self.assertEqual(self.statuses(result)["qualidade_texto"], "pass")

    def test_accepts_inline_dimensions_within_standard(self):
        content = (
            "<p>Texto sobre videogame.</p>"
            '<figure class="aligncenter"><img src="https://media.example/a.webp" width="1280" height="720" alt="Jogo importante" /></figure>'
            '<figure class="aligncenter"><img src="https://media.example/b.webp" width="900" height="506" alt="Jogo importante" /></figure>'
        )
        result = self._run_checklist(content=content)
        item = next(i for i in result["items"] if i["name"] == "dimensoes_imagens")
        self.assertEqual(item["status"], "pass", item["detail"])

    def test_rejects_inline_dimensions_outside_standard(self):
        content = (
            "<p>Texto sobre videogame.</p>"
            '<figure class="aligncenter"><img src="https://media.example/a.webp" width="500" height="300" alt="Jogo importante" /></figure>'
            '<figure class="aligncenter"><img src="https://media.example/b.webp" alt="Jogo importante" /></figure>'
        )
        result = self._run_checklist(content=content)
        item = next(i for i in result["items"] if i["name"] == "dimensoes_imagens")
        self.assertEqual(item["status"], "fail")
        self.assertIn("500x300", item["detail"])
        self.assertIn("sem width/height", item["detail"])
    def test_vision_gate_skipped_when_disabled(self):
        content = (
            "<p>Texto sobre videogame.</p>"
            '<figure class="aligncenter"><img src="https://media.example/a.webp" width="1280" height="720" alt="Jogo importante" /></figure>'
        )
        with mock.patch(
            "unicornio_editor.checklist.verify_image_subject", return_value=(True, "ok")
        ) as verify:
            result = self._run_checklist(content=content)
        item = next(i for i in result["items"] if i["name"] == "imagens_visao")
        self.assertEqual(item["status"], "skip")
        verify.assert_not_called()

    def test_vision_gate_blocks_when_model_denies_subject(self):
        content = (
            "<p>Texto sobre o jogo Redfall e seu lançamento.</p>"
            '<figure class="aligncenter"><img src="https://media.example/a.webp" width="1280" height="720" alt="Redfall key art" /></figure>'
            "<p>Mais texto sobre Redfall e jogos.</p>"
            '<figure class="aligncenter"><img src="https://media.example/b.webp" width="1280" height="720" alt="Redfall key art" /></figure>'
            '<p>Fonte: <a href="https://source.example/news" rel="nofollow noopener">Source</a>.</p>'
            "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
        )
        editorial = editorial_payload()
        editorial["seo"] = {
            "title": "Redfall ganha data de lançamento",
            "meta_description": "Uma descrição suficientemente longa sobre o conteúdo de videogame, seus detalhes, plataformas e contexto para o leitor entender a notícia.",
            "focus_keyword": "Redfall",
        }
        post = make_post(meta={"original_link": "https://source.example/news"})
        config = Config(
            "wordpress",
            "http://wp.test",
            "/wp-json/wp/v2",
            dry_run=True,
            vision_enabled=True,
            vision_api_key="k",
            vision_base_url="http://vision.test/v1",
            vision_model="vision-m",
        )
        with mock.patch(
            "unicornio_editor.checklist.verify_image_subject",
            side_effect=[(True, "ok"), (False, "modelo de visao NEGOU o assunto")],
        ):
            with tempfile.TemporaryDirectory() as directory:
                backup_path = Path(directory) / "backups" / "42" / "snapshot.json"
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                backup_path.write_text("{}")
                result = run_pre_publish_checklist(
                    post=post,
                    editorial=editorial,
                    content=content,
                    backup_path=backup_path,
                    config=config,
                    client=FakeClient(),
                )
        item = next(i for i in result["items"] if i["name"] == "imagens_visao")
        self.assertEqual(item["status"], "fail")
        self.assertIn("NEGOU", item["detail"])
        self.assertFalse(result["all_passed"])

    def test_vision_gate_skipped_when_earlier_gate_failed(self):
        content = "<p>Texto sobre videogame sem imagem.</p>"
        config = Config(
            "wordpress",
            "http://wp.test",
            "/wp-json/wp/v2",
            dry_run=True,
            vision_enabled=True,
            vision_api_key="k",
            vision_base_url="http://vision.test/v1",
            vision_model="vision-m",
        )
        with mock.patch("unicornio_editor.checklist.verify_image_subject") as verify:
            with tempfile.TemporaryDirectory() as directory:
                backup_path = Path(directory) / "backups" / "42" / "snapshot.json"
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                backup_path.write_text("{}")
                result = run_pre_publish_checklist(
                    post=make_post(meta={"original_link": "https://source.example/news"}),
                    editorial=editorial_payload(),
                    content=content,
                    backup_path=backup_path,
                    config=config,
                    client=FakeClient(),
                )
        item = next(i for i in result["items"] if i["name"] == "imagens_visao")
        self.assertEqual(item["status"], "skip")
        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
