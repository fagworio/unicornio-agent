"""Testes de sanitizacao de texto e dedupe de figuras de credito."""

import unittest

from unicornio_editor.media.text import dedupe_credit_figures, plain_text, sanitize_title


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
