"""Testes do enriquecimento determinístico de links internos."""

import unittest

from unicornio_editor.config import Config
from unicornio_editor.internal_links import add_internal_links
from unicornio_editor.workflow import compose_final_content

PS5 = "https://www.unicorniohater.com.br/games/playstation/"
XBOX = "https://www.unicorniohater.com.br/games/x-box/"
PC = "https://www.unicorniohater.com.br/games/pc/"
NETFLIX = "https://www.unicorniohater.com.br/netflix/"
ANIMES = "https://www.unicorniohater.com.br/animes/"


def editorial(html):
    return {
        "cleaned_html": html,
        "site_relevance": {"decision": "process", "confidence": 0.99},
        "seo": {"title": "Título", "focus_keyword": "videogame"},
        "media_plan": [],
        "needs_trailer": False,
        "trailer_url": None,
        "game_name": None,
    }


class InternalLinksTests(unittest.TestCase):
    def config(self, enabled=True):
        return Config(
            "wordpress", "http://wp.test", "/wp-json/wp/v2",
            dry_run=True, internal_links_enabled=enabled,
        )

    def test_internal_links_are_plain_follow_no_target_blank(self):
        out = add_internal_links("PlayStation 5 e PC.")
        self.assertIn(f'<a href="{PS5}">PlayStation 5</a>', out)
        self.assertIn(f'<a href="{PC}">PC</a>', out)
        # Links internos nao tem target/_blank nem rel nofollow.
        self.assertNotIn("target=", out)
        self.assertNotIn("rel=", out)

    def test_most_specific_term_wins(self):
        out = add_internal_links("PlayStation 5, PlayStation e PS4.")
        # A primeira ocorrencia usa o termo mais especifico; a URL so uma vez.
        self.assertEqual(out.count(f'href="{PS5}"'), 1)
        self.assertIn(">PlayStation 5<", out)

    def test_no_link_in_middle_of_word(self):
        out = add_internal_links("PlayStationjunkie e Netflixlandia.")
        self.assertNotIn("<a", out)

    def test_does_not_touch_existing_anchor(self):
        html = '<a href="https://externo.com">Netflix oficial</a> e Netflix.'
        out = add_internal_links(html)
        self.assertEqual(out.count("<a"), 2)  # existente + novo link interno
        # O texto dentro do <a> existente nao vira link interno.
        self.assertIn('<a href="https://externo.com">Netflix oficial</a>', out)
        self.assertIn(f'href="{NETFLIX}"', out)

    def test_no_links_inside_headings(self):
        html = "<h2>Análise do novo filme da Netflix</h2><p>Netflix lançou.</p>"
        out = add_internal_links(html)
        # O Netflix dentro do H2 nao foi linkado; o do paragrafo sim.
        self.assertEqual(out.count(f'href="{NETFLIX}"'), 1)
        self.assertIn("<h2>Análise do novo filme da Netflix</h2>", out)

    def test_max_once_per_url(self):
        out = add_internal_links("Netflix e Netflix e mais Netflix.")
        self.assertEqual(out.count(f'href="{NETFLIX}"'), 1)

    def test_context_gated_terms_not_auto_linked(self):
        # Android/iOS isolados, "max" isolado e "manga" isolado NAO sao linkados.
        out = add_internal_links("jogos para Android e iOS. HBO Max e max. mangá e manga.")
        self.assertIn(">HBO Max<", out)
        # "max" isolado permanece sem link (o <a> de HBO Max nao o contem).
        self.assertIn("e max.", out)
        # "manga" sem acento (contexto ambíguo) nao recebe link.
        self.assertIn("e manga.", out)
        self.assertEqual(out.count("<a"), 2)  # apenas HBO Max e mangá linkados

    def test_anime_netflix_links_naturally(self):
        out = add_internal_links("Um anime da Netflix.")
        self.assertIn(f'href="{ANIMES}"', out)
        self.assertIn(f'href="{NETFLIX}"', out)

    def test_disabled_config_keeps_body_unchanged(self):
        html = "<p>PlayStation 5 e Netflix.</p>"
        content, trailer = compose_final_content(
            editorial(html), self.config(enabled=False), None
        )
        # Sem links internos no corpo; o footer canonico do CTA continua la.
        self.assertNotIn(PS5, content)
        self.assertNotIn(NETFLIX, content)
        self.assertIn("Confira mais novidades em nosso Portal de", content)

    def test_enabled_config_adds_internal_links_in_final_content(self):
        html = "<p>PlayStation 5 e Netflix.</p>"
        content, trailer = compose_final_content(
            editorial(html), self.config(enabled=True), None
        )
        self.assertIn(f'href="{PS5}"', content)
        self.assertIn(f'href="{NETFLIX}"', content)


if __name__ == "__main__":
    unittest.main()
