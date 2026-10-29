"""Testes de sanitizacao de texto e dedupe de figuras de credito."""

import unittest

from unicornio_editor.media.text import (
    credit_text_with_source_site,
    dedupe_credit_figures,
    plain_text,
    sanitize_title,
    source_site_name,
)


class PlainTextTests(unittest.TestCase):
    def test_strips_paragraph_tags(self):
        self.assertEqual(plain_text("<p>Credit: Nintendo</p>"), "Credit: Nintendo")

    def test_decodes_escaped_html(self):
        self.assertEqual(plain_text("&lt;p&gt;Credit: Nintendo&lt;/p&gt;"), "<p>Credit: Nintendo</p>")

    def test_strips_tags_keeps_meaning(self):
        value = "Crédito da imagem: Nintendo (via Nintendo Life). <a href=\"x\">Screenshot</a> oficial."
        self.assertEqual(
            plain_text(value),
            "Crédito da imagem: Nintendo (via Nintendo Life). Screenshot oficial.",
        )

    def test_normalizes_whitespace_and_none(self):
        self.assertEqual(plain_text("  a\n\n  b  "), "a b")
        self.assertEqual(plain_text(None), "")

    def test_sanitize_title_decodes_complete_and_legacy_amp_entities(self):
        title = "Pokémon Heart &amp; Soul 2.0: freebie gratuito"
        self.assertEqual(sanitize_title(title), "Pokémon Heart & Soul 2.0: freebie gratuito")
        self.assertEqual(sanitize_title("Heart &amp Soul"), "Heart & Soul")


class SourceCreditTests(unittest.TestCase):
    def test_source_name_comes_from_page_host_not_image_cdn_or_query(self):
        self.assertEqual(
            source_site_name("https://www.IGN.com/article?token=private"), "IGN",
        )
        self.assertEqual(
            source_site_name("https://images.cdn.nintendo.com.br/art?sig=private"),
            "Nintendo",
        )

    def test_site_credit_appends_without_discarding_existing_author_caption(self):
        self.assertEqual(
            credit_text_with_source_site(
                "Crédito da imagem: E2E Artist. Captura de gameplay.",
                "https://www.ign.com/article",
            ),
            "Crédito da imagem: E2E Artist. Captura de gameplay. Crédito da imagem: IGN.",
        )

    def test_existing_source_credit_is_not_duplicated(self):
        text = "Crédito da imagem: Autor, via IGN. Captura do jogo."
        self.assertEqual(credit_text_with_source_site(text, "https://www.ign.com/article"), text)

    def test_site_mentioned_outside_credit_clause_does_not_suppress_attribution(self):
        self.assertEqual(
            credit_text_with_source_site(
                "Crédito da imagem: Autor. O artigo também menciona IGN.",
                "https://www.ign.com/article",
            ),
            "Crédito da imagem: Autor. O artigo também menciona IGN. Crédito da imagem: IGN.",
        )

    def test_safe_persisted_site_name_adds_credit_without_url_material(self):
        self.assertEqual(
            credit_text_with_source_site("", "https://user:secret@www.ign.com/article?token=private"),
            "Crédito da imagem: IGN.",
        )

    def test_missing_or_invalid_source_does_not_invent_credit(self):
        self.assertEqual(credit_text_with_source_site("Legenda editorial.", ""), "Legenda editorial.")
        self.assertEqual(credit_text_with_source_site("", "javascript://ign.com/image"), "")


class DedupeCreditFiguresTests(unittest.TestCase):
    def test_removes_orphan_duplicate_credit(self):
        html = (
            '<figure class="aligncenter"><img src="https://x/a.webp" alt="A" />'
            "<figcaption>Crédito da imagem: Nintendo. Uso com crédito.</figcaption></figure>"
            '<figure class="aligncenter"><figcaption>Crédito da imagem: Nintendo. Uso com crédito.</figcaption></figure>'
            "<p>Texto.</p>"
        )
        result = dedupe_credit_figures(html)
        self.assertEqual(result.count("<figcaption>Crédito da imagem: Nintendo. Uso com crédito.</figcaption>"), 1)
        self.assertEqual(result.count("<figure class=\"aligncenter\">"), 1)

    def test_keeps_figure_with_image(self):
        html = (
            '<figure class="aligncenter"><img src="https://x/a.webp" alt="A" />'
            "<figcaption>Crédito da imagem: Nintendo. Uso com crédito.</figcaption></figure>"
        )
        self.assertEqual(dedupe_credit_figures(html), html)

    def test_keeps_distinct_credits(self):
        html = (
            '<figure class="aligncenter"><img src="https://x/a.webp" alt="A" />'
            "<figcaption>Crédito da imagem: Nintendo.</figcaption></figure>"
            '<figure class="aligncenter"><img src="https://x/b.webp" alt="B" />'
            "<figcaption>Crédito da imagem: Sony.</figcaption></figure>"
        )
        self.assertEqual(dedupe_credit_figures(html), html)

    def test_handles_empty(self):
        self.assertEqual(dedupe_credit_figures(""), "")
        self.assertEqual(dedupe_credit_figures(None), "")


if __name__ == "__main__":
    unittest.main()
