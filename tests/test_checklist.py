import tempfile
import unittest
from pathlib import Path

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
        return {
            "id": media_id,
            "source_url": "https://media.example/featured.webp",
            "media_details": {"width": 1200, "height": 720},
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
            backup_path = Path(directory) / "snapshot.json"
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
            '<figure class="aligncenter"><img src="https://media.example/a.webp" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            "<p>Texto revisado sobre o jogo videogame.</p>"
            '<figure class="aligncenter"><img src="https://media.example/b.webp" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            "<p>Mais texto sobre videogame e o lançamento.</p>"
            '<figure class="aligncenter"><img src="https://media.example/c.webp" alt="Redfall key art" />'
            "<figcaption>Crédito da imagem: Autor. Redfall. CC BY 4.0.</figcaption></figure>"
            "<p>Fechando o texto sobre videogame.</p>"
            '<figure class="aligncenter"><img src="https://media.example/d.webp" alt="Redfall key art" />'
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

    def test_body_images_waived_when_no_image_available(self):
        # Policy: absence beats a wrong image — 0 images does not block.
        result = self._run_checklist()
        self.assertEqual(self.statuses(result)["imagens_no_corpo"], "pass")

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
            backup_path = Path(directory) / "snapshot.json"
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
            backup_path = Path(directory) / "snapshot.json"
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


if __name__ == "__main__":
    unittest.main()
