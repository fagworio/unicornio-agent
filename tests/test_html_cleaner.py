import unittest

from unicornio_editor.html_cleaner import clean_html


class HtmlCleanerTests(unittest.TestCase):
    def test_unwraps_article_and_div_but_preserves_content(self):
        result = clean_html("<article><div><p>Texto <strong>importante</strong>.</p></div></article>")
        self.assertEqual(result, "<p>Texto <strong>importante</strong>.</p>")

    def test_removes_imported_images_and_dangerous_elements(self):
        result = clean_html(
            '<p onclick="bad()">Antes<img src="legacy.jpg" alt="old">depois</p>'
            '<script>alert(1)</script><iframe src="bad"></iframe>'
        )
        self.assertEqual(result, '<p>Antesdepois</p>')

    def test_removes_javascript_links_and_event_attributes(self):
        result = clean_html(
            '<a href="javascript:alert(1)" onmouseover="bad" class="link">Link</a>'
            '<a href="https://example.com" target="_blank">Seguro</a>'
        )
        self.assertEqual(
            result,
            '<a class="link">Link</a><a href="https://example.com" target="_blank">Seguro</a>',
        )

    def test_removes_old_canonical_cta_and_source(self):
        result = clean_html(
            "<p>Notícia.</p><h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
            '<em>Fonte: <a href="https://old.example">Site</a></em>'
        )
        self.assertEqual(result, "<p>Notícia.</p>")

    def test_keeps_image_inside_figure_with_complete_credit(self):
        result = clean_html(
            '<figure class="aligncenter"><img src="https://s3.example/img.webp" alt="Cena" />'
            "<figcaption>Crédito da imagem: Autor (via Wikimedia Commons). Cena do jogo. "
            "Licença CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0).</figcaption>"
            "</figure>"
        )
        self.assertIn('<img src="https://s3.example/img.webp" alt="Cena" />', result)
        self.assertIn("Crédito da imagem: Autor", result)

    def test_keeps_image_with_cc0_license(self):
        result = clean_html(
            '<figure><img src="https://s3.example/foto.webp" alt="Foto" />'
            "<figcaption>Crédito da imagem: Foto. Licença CC0 "
            "(http://creativecommons.org/publicdomain/zero/1.0/deed.en).</figcaption></figure>"
        )
        self.assertIn('<img src="https://s3.example/foto.webp" alt="Foto" />', result)

    def test_drops_image_when_credit_has_no_license(self):
        result = clean_html(
            '<figure><img src="https://s3.example/sem.webp" alt="Sem licença" />'
            "<figcaption>Crédito da imagem: Autor. Sem informação de licença.</figcaption></figure>"
        )
        self.assertNotIn("<img", result)
        self.assertIn("Crédito da imagem: Autor", result)

    def test_drops_image_when_figure_has_no_figcaption(self):
        result = clean_html(
            '<figure class="aligncenter"><img src="https://s3.example/x.webp" alt="x" /></figure>'
        )
        self.assertNotIn("<img", result)
        self.assertEqual(result, '<figure class="aligncenter"></figure>')

    def test_strips_event_attributes_from_kept_image(self):
        result = clean_html(
            '<figure><img src="https://s3.example/s.webp" alt="a" onclick="bad()" />'
            "<figcaption>Crédito da imagem: Autor. Licença CC BY 4.0 "
            "(https://creativecommons.org/licenses/by/4.0).</figcaption></figure>"
        )
        self.assertNotIn("onclick", result)
        self.assertIn('<img src="https://s3.example/s.webp" alt="a" />', result)


if __name__ == "__main__":
    unittest.main()
