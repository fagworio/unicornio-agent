import unittest

from unicornio_editor.media.relevance import (
    extract_entities,
    image_is_relevant,
    iter_content_images,
)


class RelevanceTests(unittest.TestCase):
    def test_extract_entities_keeps_work_name_and_drops_concepts(self):
        entities = extract_entities(
            title="Redfall ganha novo trailer com vampiros",
            focus_keyword="Redfall",
            game_name="Redfall",
        )
        self.assertIn("redfall", entities)
        for junk in ("vampiros", "trailer", "ganha", "novo"):
            self.assertNotIn(junk, entities)

    def test_bat_photo_rejected_for_vampire_game(self):
        entities = extract_entities(title="Redfall mostra novos vampiros", game_name="Redfall")
        self.assertFalse(
            image_is_relevant(
                alt_text="Morcego real em voo noturno",
                credit_text="Crédito da imagem: Fotógrafo. Morcego. Licença CC0 (http://creativecommons.org/publicdomain/zero/1.0/deed.en).",
                source_url="https://commons.wikimedia.org/wiki/File:Bat_in_flight.jpg",
                entities=entities,
            )
        )

    def test_game_key_art_accepted(self):
        entities = extract_entities(title="Redfall mostra novos vampiros", game_name="Redfall")
        self.assertTrue(
            image_is_relevant(
                alt_text="Redfall key art oficial",
                credit_text="Crédito da imagem: Arkane. Redfall. CC BY 4.0.",
                source_url="https://bethesda.net/games/redfall",
                entities=entities,
            )
        )

    def test_generic_convention_photo_rejected_for_anime_post(self):
        entities = extract_entities(
            title="Oshi no Ko Season 4 ganha teaser visual e se aproxima da Final Season",
            content_html="<p>Aqua e Ruby seguem em busca de respostas.</p>",
        )
        self.assertIn("oshi", entities)
        self.assertFalse(
            image_is_relevant(
                alt_text="Público em convenção de anime",
                credit_text="Crédito da imagem: Nicholas Moreau (via Wikimedia Commons). Público em convenção de anime. CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0).",
                source_url="https://commons.wikimedia.org/wiki/File:Convention_crowd.jpg",
                entities=entities,
            )
        )

    def test_anime_key_visual_accepted(self):
        entities = extract_entities(
            title="Oshi no Ko Season 4 ganha teaser visual e se aproxima da Final Season",
            game_name="Oshi no Ko",
        )
        self.assertTrue(
            image_is_relevant(
                alt_text="Oshi no Ko Season 4 key visual",
                credit_text="Crédito da imagem: Oshi no Ko Production Committee. Oshi no Ko. CC BY 4.0.",
                source_url="https://example.com/oshi-no-ko-season4.jpg",
                entities=entities,
            )
        )

    def test_quoted_work_name_from_content(self):
        entities = extract_entities(
            title="Novo jogo da Compulsion Games anunciado",
            content_html='<p>O estúdio revelou <strong>"South of Midnight"</strong> para 2026.</p>',
        )
        self.assertTrue(
            image_is_relevant(
                alt_text="South of Midnight key art",
                credit_text="South of Midnight. CC BY 4.0.",
                source_url="https://example.com/south-of-midnight.jpg",
                entities=entities,
            )
        )

    def test_iter_content_images_figures_and_bare(self):
        content = (
            '<figure class="aligncenter"><img src="https://m.example/a.webp" alt="Cena A" />'
            "<figcaption>Crédito da imagem: Autor. Cena A. CC BY 4.0.</figcaption></figure>"
            '<img src="https://m.example/b.webp" alt="Solto" />'
        )
        images = iter_content_images(content)
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]["src"], "https://m.example/a.webp")
        self.assertEqual(images[0]["alt"], "Cena A")
        self.assertIn("Autor", images[0]["caption"])
        self.assertEqual(images[1]["alt"], "Solto")


if __name__ == "__main__":
    unittest.main()
