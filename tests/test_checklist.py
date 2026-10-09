import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unicornio_editor.checklist import required_image_count, required_image_count_for_content, run_pre_publish_checklist
from unicornio_editor.config import Config
from unicornio_editor.media.vision_gate import VisionGateError


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
    def test_listicle_required_is_maximum_of_word_and_item_requirements(self):
        content = "".join(
            f"<h2>{index}. Item {index}</h2><p>Descrição do item {index}.</p>"
            for index in range(1, 6)
        )
        self.assertEqual(required_image_count(1200, title="Top 5 jogos", content=content), 6)

    def test_required_image_count_for_content_is_the_canonical_body_contract(self):
        def body(words):
            return "<p>" + " ".join("palavra" for _ in range(words)) + "</p>"

        self.assertEqual(required_image_count_for_content(body(599)), 2)
        self.assertEqual(required_image_count_for_content(body(600)), 2)
        self.assertEqual(required_image_count_for_content(body(601)), 4)
        self.assertEqual(required_image_count_for_content(body(1000)), 4)
        self.assertEqual(required_image_count_for_content(body(1001)), 6)

    def test_required_image_count_for_content_preserves_listicle_floor(self):
        content = "<h2>1. Jogo A</h2><h2>2. Jogo B</h2><h2>3. Jogo C</h2>"
        self.assertEqual(required_image_count_for_content(content, title="Top jogos"), 3)

    def config(self):
        return Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=True)

    def _run_checklist(self, post=None, editorial=None, content=None, backup=True, client=None, media_context=None):
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
                media_context=media_context,
            )

    def statuses(self, result):
        return {item["name"]: item["status"] for item in result["items"]}

    def test_language_gate_blocks_unequivocal_english_body(self):
        result = self._run_checklist(
            post=make_post(title={"raw": "New game release announced"}),
            editorial=editorial_payload(
                **{
                    "seo": {
                        "title": "New game release announced",
                        "meta_description": "The studio announced the latest news about the game and its release date.",
                        "focus_keyword": "new game release",
                    }
                }
            ),
            content=(
                "The studio announced a new release for the game. Players will receive "
                "more details about the upcoming episode and film. The latest news "
                "confirms the release date for the show."
            ),
        )
        idioma = next(item for item in result["items"] if item["name"] == "idioma_pt_br")
        assert idioma["status"] == "fail"
        assert result["language"]["language"] == "en"

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

    def test_normal_article_rejects_featured_repeated_inline(self):
        featured_url = "https://media.example/redfall-1280x720.webp"
        content = (
            f'<figure><img src="{featured_url}" alt="Redfall key art" /></figure>'
            "<p>Texto revisado sobre o jogo videogame.</p>"
        )
        result = self._run_checklist(content=content)
        self.assertEqual(self.statuses(result)["featured_inline_position"], "fail")

    def test_normal_article_rejects_inline_visually_equal_to_featured(self):
        featured_url = "https://media.example/redfall-1280x720.webp"
        inline_url = "https://other.example/redfall-copy.webp"
        content = (
            f'<figure><img src="{inline_url}" alt="Redfall key art" /></figure>'
            "<p>Texto revisado sobre o jogo videogame.</p>"
        )
        with mock.patch("unicornio_editor.media.visual_hash.image_hashes", return_value={}):
            with mock.patch(
                "unicornio_editor.media.visual_hash.similar_image_pairs",
                return_value=[(featured_url, inline_url, 2)],
            ):
                result = self._run_checklist(content=content)
        self.assertEqual(self.statuses(result)["featured_inline_position"], "fail")

    def test_listicle_allows_featured_once_only_as_last_inline_image(self):
        featured_url = "https://media.example/redfall-1280x720.webp"
        content = (
            "<h2>1. Item um</h2><p>Descrição do primeiro item.</p>"
            '<figure><img src="https://media.example/item-um.webp" alt="videogame" /></figure>'
            "<h2>2. Item dois</h2><p>Descrição do segundo item.</p>"
            f'<figure><img src="{featured_url}" alt="Redfall key art" /></figure>'
        )
        result = self._run_checklist(
            post=make_post(title={"raw": "Top 2 jogos"}), content=content
        )
        self.assertEqual(self.statuses(result)["featured_inline_position"], "pass")

    def test_listicle_rejects_featured_when_not_final_inline_image(self):
        featured_url = "https://media.example/redfall-1280x720.webp"
        content = (
            "<h2>1. Item um</h2><p>Descrição do primeiro item.</p>"
            f'<figure><img src="{featured_url}" alt="Redfall key art" /></figure>'
            "<h2>2. Item dois</h2><p>Descrição do segundo item.</p>"
            '<figure><img src="https://media.example/item-dois.webp" alt="videogame" /></figure>'
        )
        result = self._run_checklist(
            post=make_post(title={"raw": "Top 2 jogos"}), content=content
        )
        self.assertEqual(self.statuses(result)["featured_inline_position"], "fail")

    def test_listicle_final_featured_variant_is_identity_valid_but_not_quota(self):
        content = (
            "<h2>1. Jogo A</h2><p>Descrição.</p><img src=\"https://media.example/other.webp\">"
            "<h2>2. Jogo B</h2><p>Descrição.</p><img src=\"https://media.example/crop.webp\">"
        )
        media_context = {
            "visual_identity_policy": 4,
            "inline": {"required": 2, "accepted": [
                {"media_id": 8, "media_url": "https://media.example/other.webp", "slot": 0, "sha256": "a", "phash": "b", "visual_group_id": "v:a", "visual_verification": {"decision": "DIFFERENT"}},
                {"media_id": 9, "media_url": "https://media.example/crop.webp", "slot": 1, "sha256": "c", "phash": "d", "visual_group_id": "v:c", "visual_verification": {"decision": "SAME_ART_CROP", "duplicate_of": "7"}},
            ]},
            "featured": {"status": "valid", "media_id": 7, "media_url": "https://media.example/redfall-1280x720.webp", "sha256": "z", "phash": "y", "visual_group_id": "v:z", "visual_verification": {"decision": "INITIAL"}},
        }
        result = self._run_checklist(content=content, media_context=media_context)
        statuses = self.statuses(result)
        self.assertEqual(statuses["visual_identity_verified"], "pass")
        self.assertEqual(statuses["media_duplicate_confirmed"], "pass")
        self.assertEqual(statuses["imagens_no_corpo"], "fail")

    def test_html_semantics_rejects_internal_h1_and_heading_skip(self):
        content = "<h1>Título interno</h1><h3>Subseção</h3><p>Texto sobre videogame.</p>"
        result = self._run_checklist(content=content)
        item = next(item for item in result["items"] if item["name"] == "html_semantics")
        self.assertEqual(item["status"], "fail")
        self.assertIn("H1 interno", item["detail"])

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
        self.assertEqual(self.statuses(result)["qualidade_texto"], "pass")

    def test_media_required_is_canonical_after_compose_changes_word_count(self):
        content = "<p>" + ("palavra videogame noticia lancamento " * 155) + "</p>"
        result = self._run_checklist(
            content=content,
            media_context={"inline": {"required": 2, "accepted": []}},
        )
        item = next(i for i in result["items"] if i["name"] == "imagens_no_corpo")
        self.assertIn(">= 2 imagens", item["detail"])

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

    def test_media_failure_identifies_the_invalid_asset(self):
        content = (
            '<figure class="aligncenter"><img src="https://media.example/morcego.webp" alt="Morcego real em voo" />'
            "<figcaption>Crédito da imagem: Fotógrafo. Morcego real. CC0.</figcaption></figure>"
            "<p>Texto revisado sobre o jogo videogame.</p>"
        )
        result = self._run_checklist(
            content=content,
            media_context={"inline": {"accepted": [
                {"media_id": 11, "media_url": "https://media.example/morcego.webp", "slot": 0},
            ]}},
        )
        item = next(i for i in result["items"] if i["name"] == "relevancia_imagens")
        assert item["invalid_media"] == [{
            "url": "https://media.example/morcego.webp", "media_id": 11, "slot": 0,
        }]

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

    def test_trailer_skips_with_audited_unavailable_waiver(self):
        editorial = editorial_payload(
            game_name="Meu Jogo",
            trailer_unavailable=True,
            trailer_search_evidence={
                "query": "Meu Jogo trailer",
                "provider": "youtube",
                "searched_at": "2026-09-03T12:00:00+00:00",
                "result": "official_not_found",
            },
        )
        result = self._run_checklist(editorial=editorial)
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

    def test_vision_gate_blocks_when_model_denies_featured(self):
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
            return_value=(False, "modelo de visao NEGOU o assunto"),
        ), mock.patch(
            "unicornio_editor.checklist.prepare_vision_image_input",
            return_value="data:image/png;base64,AAAA",
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

    def test_vision_provider_error_is_explicit_media_provider_failure(self):
        content = (
            "<p>Texto sobre o jogo Redfall e seu lançamento.</p>"
            '<figure><img src="https://media.example/a.webp" width="1280" height="720" alt="Redfall key art" /></figure>'
            '<figure><img src="https://media.example/b.webp" width="1280" height="720" alt="Redfall key art" /></figure>'
            "<p>Fonte: <a href=\"https://source.example/news\" rel=\"nofollow noopener\">Source</a>.</p>"
            "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
        )
        editorial = editorial_payload()
        editorial["seo"] = {
            "title": "Redfall ganha data de lançamento",
            "meta_description": "Uma descrição suficientemente longa sobre o conteúdo de videogame, seus detalhes, plataformas e contexto para o leitor entender a notícia.",
            "focus_keyword": "Redfall",
        }
        config = Config(
            "wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=True,
            vision_enabled=True, vision_api_key="k", vision_base_url="http://vision.test/v1", vision_model="vision-m",
        )
        with mock.patch(
            "unicornio_editor.checklist.verify_image_subject",
            side_effect=VisionGateError("API de visao respondeu HTTP 400"),
        ), mock.patch(
            "unicornio_editor.checklist.prepare_vision_image_input",
            return_value="data:image/png;base64,AAAA",
        ):
            with tempfile.TemporaryDirectory() as directory:
                backup_path = Path(directory) / "backups" / "42" / "snapshot.json"
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                backup_path.write_text("{}")
                result = run_pre_publish_checklist(
                    post=make_post(meta={"original_link": "https://source.example/news"}),
                    editorial=editorial,
                    content=content,
                    backup_path=backup_path,
                    config=config,
                    client=FakeClient(),
                )
        item = next(i for i in result["items"] if i["name"] == "imagens_visao")
        self.assertEqual(item["blocker"], "provider_error")
        self.assertEqual(item["phase"], "media")

    def test_vision_gate_verifies_only_featured(self):
        # Inline NAO paga visao: apenas a featured e verificada (1 chamada),
        # mesmo com varias imagens inline no conteudo — corte de custo.
        content = (
            "<p>Texto sobre o jogo Redfall e seu lançamento.</p>"
            '<figure class="aligncenter"><img src="https://media.example/a.webp" width="1280" height="720" alt="Redfall key art" /></figure>'
            '<figure class="aligncenter"><img src="https://media.example/b.webp" width="1280" height="720" alt="Redfall key art" /></figure>'
            "<p>Mais texto sobre Redfall e jogos.</p>"
            '<p>Fonte: <a href="https://source.example/news" rel="nofollow noopener">Source</a>.</p>'
            "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
        )
        editorial = editorial_payload()
        editorial["seo"] = {
            "title": "Redfall ganha data de lançamento",
            "meta_description": "Uma descrição suficientemente longa sobre o conteúdo de videogame, seus detalhes, plataformas e contexto para o leitor entender a notícia.",
            "focus_keyword": "Redfall",
        }
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
            "unicornio_editor.checklist.verify_image_subject", return_value=(True, "ok")
        ) as verify, mock.patch(
            "unicornio_editor.checklist.prepare_vision_image_input",
            return_value="data:image/png;base64,AAAA",
        ):
            with tempfile.TemporaryDirectory() as directory:
                backup_path = Path(directory) / "backups" / "42" / "snapshot.json"
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                backup_path.write_text("{}")
                run_pre_publish_checklist(
                    post=make_post(meta={"original_link": "https://source.example/news"}),
                    editorial=editorial,
                    content=content,
                    backup_path=backup_path,
                    config=config,
                    client=FakeClient(),
                )
        self.assertEqual(verify.call_count, 1)
        self.assertTrue(verify.call_args.kwargs["image_url"].startswith("data:image/"))

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

    def test_media_exhausted_do_editorial_nao_concede_waiver(self):
        """Acceptance 13: `media_exhausted` vindo do JSON do LLM NAO vale.

        A exaustão da busca só pode ser declarada pelo CÓDIGO, com evidência
        (queries executadas, candidatos verificados, frames distintos). Antes
        bastava o editorial declarar o flag para dispensar o mínimo 2/4/6 de um
        artigo normal — e `attempts` de apply era lido como "busca esgotada".
        """
        editorial = editorial_payload(media_exhausted=True)
        content = (
            "<p>Texto sobre o jogo videogame e seu lançamento.</p>"
            "<p>Mais texto sobre videogame.</p>"
            '<p>Fonte: <a href="https://source.example/news" rel="nofollow noopener">Source</a>.</p>'
            "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
        )
        result = self._run_checklist(
            post=make_post(meta={"original_link": "https://source.example/news"}),
            editorial=editorial,
            content=content,
        )
        item = next(i for i in result["items"] if i["name"] == "imagens_no_corpo")
        self.assertEqual(item["status"], "fail")
        self.assertNotIn("waived", item.get("detail") or "")

    def test_deterministic_search_exhaustion_does_not_waive_inline_for_non_listicle(self):
        content = (
            "<p>Texto revisado sobre o jogo videogame e seu lançamento.</p>"
            "<p>Mais informações sobre videogame para o leitor.</p>"
            "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
        )
        result = self._run_checklist(
            post=make_post(featured_media=7),
            content=content,
            media_context={
                "search": {
                    "completed": True,
                    "exhausted": True,
                    "queries_attempted": 2,
                    "engines_attempted": ["bing", "yandex"],
                    "candidates_seen": 8,
                    "candidates_rejected": 7,
                    "distinct_valid_frames": 1,
                }
            },
        )
        item = next(i for i in result["items"] if i["name"] == "imagens_no_corpo")
        self.assertEqual(item["status"], "fail")
        self.assertFalse(result["media_decision"]["waiver_applied"])

    def test_second_enrichment_round_does_not_waive_normal_article_without_faking_missing(self):
        content = (
            "<p>Texto revisado sobre o jogo videogame e seu lançamento.</p>"
            "<p>Mais informações sobre videogame para o leitor.</p>"
            "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
        )
        result = self._run_checklist(
            post=make_post(featured_media=7),
            content=content,
            media_context={
                "enrichment_round": 2,
                "inline": {"required": 2, "accepted": []},
                "search": {
                    "completed": False,
                    "exhausted": False,
                    "completion_reason": "INTERRUPTED",
                },
            },
        )
        item = next(i for i in result["items"] if i["name"] == "imagens_no_corpo")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(result["media_decision"]["missing"], 2)
        self.assertFalse(result["media_decision"]["waiver_applied"])
        self.assertFalse(result["media_decision"]["search_completed"])
        self.assertFalse(result["media_decision"]["search_exhausted"])
        self.assertEqual(result["media_decision"]["search_completion_reason"], "INTERRUPTED")

    def test_search_outcomes_never_waive_required_inline_images(self):
        content = (
            "<p>Texto revisado sobre o jogo videogame e seu lançamento.</p>"
            "<p>Mais informações sobre videogame para o leitor.</p>"
            "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
        )
        cases = [
            ("PROVIDER_ERROR", 2, False, False),
            ("EXHAUSTED", 1, True, True),
            ("TARGET_REACHED", 1, True, False),
        ]
        for reason, enrichment_round, completed, exhausted in cases:
            result = self._run_checklist(
                post=make_post(featured_media=7),
                content=content,
                media_context={
                    "enrichment_round": enrichment_round,
                    "search": {
                        "completed": completed,
                        "exhausted": exhausted,
                        "completion_reason": reason,
                    },
                },
            )
            decision = result["media_decision"]
            self.assertFalse(decision["waiver_applied"], reason)
            self.assertEqual(decision["waiver_reason"], "", reason)
            self.assertEqual(decision["search_completed"], completed, reason)
            self.assertEqual(decision["search_exhausted"], exhausted, reason)

    def test_media_exhausted_does_not_waive_listicle(self):
        # Listicle (Top N) NAO dispensa o minimo: continua exigindo imagem por
        # item (vai para awaiting_human no apply, decisao manual).
        editorial = editorial_payload(media_exhausted=True)
        content = (
            "<h2>1. Jogo: titulo</h2><p>Descricao do jogo.</p>"
            "<h2>2. Jogo: titulo</h2><p>Descricao do jogo.</p>"
            '<p>Fonte: <a href="https://source.example/news" rel="nofollow noopener">Source</a>.</p>'
            "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
        )
        result = self._run_checklist(
            post=make_post(
                title={"raw": "10 melhores jogos"},
                meta={"original_link": "https://source.example/news"},
            ),
            editorial=editorial,
            content=content,
        )
        item = next(i for i in result["items"] if i["name"] == "imagens_no_corpo")
        self.assertEqual(item["status"], "fail")

    def test_search_exhaustion_does_not_waive_listicle_with_featured(self):
        """A real search waiver must never remove a listicle's per-item gate."""
        content = (
            "<h2>1. Jogo: titulo</h2><p>Descricao do jogo.</p>"
            "<h2>2. Jogo: titulo</h2><p>Descricao do jogo.</p>"
            '<p>Fonte: <a href="https://source.example/news" rel="nofollow noopener">Source</a>.</p>'
            "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
        )
        result = self._run_checklist(
            post=make_post(
                title={"raw": "10 melhores jogos"},
                featured_media=7,
                meta={"original_link": "https://source.example/news"},
            ),
            content=content,
            media_context={
                "search": {
                    "completed": True,
                    "exhausted": True,
                    "queries_attempted": 2,
                    "engines_attempted": ["bing", "yandex"],
                    "candidates_seen": 8,
                    "candidates_rejected": 8,
                    "distinct_valid_frames": 1,
                }
            },
        )
        item = next(i for i in result["items"] if i["name"] == "imagens_no_corpo")
        self.assertEqual(item["status"], "fail")


    # ---- Fase 1: o pHash NUNCA reduz o minimo 2/4/6 ----

    def _texto_1200(self):
        return "<p>" + ("palavra videogame jogo noticia lancamento " * 250) + "</p>"

    def test_minimo_nao_e_reduzido_por_frames_distintos(self):
        """1200 palavras (minimo 6) com 2 imagens distintas -> FAIL.

        Antes `required_effective = distinct_frames` virava 2 e o post passava
        alegando que so existiam duas imagens disponiveis — o pHash apenas
        provou que as 2 URLs sao frames diferentes, nunca que a busca esgotou.
        """
        content = (
            '<figure><img src="https://media.example/a.webp" alt="Notícia sobre videogame" /></figure>'
            '<figure><img src="https://media.example/b.webp" alt="Notícia sobre videogame" /></figure>'
            + self._texto_1200()
        )
        hashes = {"https://media.example/a.webp": 1, "https://media.example/b.webp": 2}
        with mock.patch(
            "unicornio_editor.media.visual_hash.distinct_image_count", return_value=2
        ), mock.patch(
            "unicornio_editor.media.visual_hash.image_hashes", return_value=hashes
        ):
            result = self._run_checklist(content=content)
        self.assertEqual(self.statuses(result)["imagens_no_corpo"], "fail")
        item = next(i for i in result["items"] if i["name"] == "imagens_no_corpo")
        self.assertIn("6", str(item.get("detail") or ""))

    def test_seis_imagens_distintas_passam(self):
        """1200 palavras + 6 imagens distintas -> PASS (minimo honrado)."""
        imgs = "".join(
            f'<figure><img src="https://media.example/{i}.webp" alt="Notícia sobre videogame" /></figure>'
            for i in range(6)
        )
        content = imgs + self._texto_1200()
        hashes = {f"https://media.example/{i}.webp": 10 + i for i in range(6)}
        with mock.patch(
            "unicornio_editor.media.visual_hash.distinct_image_count", return_value=6
        ), mock.patch(
            "unicornio_editor.media.visual_hash.image_hashes", return_value=hashes
        ):
            result = self._run_checklist(content=content)
        self.assertEqual(self.statuses(result)["imagens_no_corpo"], "pass")


if __name__ == "__main__":
    unittest.main()
