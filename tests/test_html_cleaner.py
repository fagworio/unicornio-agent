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

    def test_removes_h1_and_its_duplicate_title_from_content(self):
        result = clean_html(
            "<h1>Título já exibido pelo WordPress</h1><p>Texto do post.</p>",
            post_title="Título já exibido pelo WordPress",
        )
        self.assertEqual(result, "<p>Texto do post.</p>")

    def test_demotes_distinct_h1_without_losing_its_text(self):
        result = clean_html("<h1>Contexto da notícia</h1><p>Texto do post.</p>", post_title="Título principal")
        self.assertEqual(result, "<h2>Contexto da notícia</h2><p>Texto do post.</p>")

    def test_keeps_image_inside_figure_with_optional_caption(self):
        result = clean_html(
            '<figure class="aligncenter"><img src="https://s3.example/img.webp" alt="Cena" />'
            "<figcaption>Crédito da imagem: Autor (via Wikimedia Commons). Cena do jogo. "
            "Licença CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0).</figcaption>"
            "</figure>"
        )
        self.assertIn('<img src="https://s3.example/img.webp" alt="Cena" />', result)
        self.assertIn("Crédito da imagem: Autor", result)

    def test_keeps_image_without_license_or_credit_metadata(self):
        result = clean_html(
            '<figure><img src="https://s3.example/sem.webp" alt="Cena" />'
            "<figcaption>Legenda editorial opcional.</figcaption></figure>"
        )
        self.assertIn('<img src="https://s3.example/sem.webp" alt="Cena" />', result)
        self.assertIn("Legenda editorial opcional.", result)

    def test_repairs_orphan_img_next_to_figure_with_optional_caption(self):
        from unicornio_editor.html_cleaner import _repair_orphan_media

        broken = (
            '<p>Texto.</p><figure class="aligncenter">'
            "<figcaption>Legenda editorial opcional.</figcaption></figure>"
            '<img width="1280" height="720" src="https://s3.example/cena.jpg" />'
        )
        cleaned = clean_html(_repair_orphan_media(broken))
        self.assertIn('<figure class="aligncenter"><img width="1280" height="720" src="https://s3.example/cena.jpg" />', cleaned)
        self.assertIn("Legenda editorial opcional.", cleaned)

    def test_repairs_orphan_img_before_figure_without_caption(self):
        from unicornio_editor.html_cleaner import _repair_orphan_media

        cleaned = clean_html(
            _repair_orphan_media('<img src="https://cdn.example/cena.jpg" /><figure></figure>')
        )
        self.assertIn('<figure><img src="https://cdn.example/cena.jpg" /></figure>', cleaned)

    def test_keeps_image_when_figure_has_no_figcaption(self):
        result = clean_html(
            '<figure class="aligncenter"><img src="https://s3.example/x.webp" alt="x" /></figure>'
        )
        self.assertIn('<img src="https://s3.example/x.webp" alt="x" />', result)

    def test_drops_unsafe_figure_image_url(self):
        result = clean_html(
            '<figure><img src="javascript:alert(1)" alt="x" /></figure>'
        )
        self.assertNotIn("<img", result)

    def test_strips_event_attributes_from_kept_image(self):
        result = clean_html(
            '<figure><img src="https://s3.example/s.webp" alt="a" onclick="bad()" />'
            "<figcaption>Crédito da imagem: Autor. Licença CC BY 4.0 "
            "(https://creativecommons.org/licenses/by/4.0).</figcaption></figure>"
        )
        self.assertNotIn("onclick", result)
        self.assertIn('<img src="https://s3.example/s.webp" alt="a" />', result)


    def test_keeps_image_inside_wp_caption_shortcode(self):
        # O shortcode nativo [caption]...[/caption] (que o WP renderiza como
        # figure com credito) deve PRESERVAR o <img> interno — nao dropar como
        # se fosse uma imagem importada fora de <figure>.
        html = (
            '[caption id="" align="aligncenter" width="1280"]'
            '<img src="https://s3.example/wp-inline.webp" width="1280" height="721" alt="Filme" /> '
            "Crédito da imagem: Autor. Filme. Uso com crédito.[/caption]"
        )
        result = clean_html(html)
        self.assertIn("<img", result)
        self.assertIn("Crédito da imagem: Autor. Filme. Uso com crédito.", result)
        self.assertIn("[caption", result)
        self.assertIn("[/caption]", result)

    def test_keeps_caption_img_with_other_content(self):
        html = (
            "<p>Texto do post.</p>"
            '[caption id="" align="aligncenter" width="1280"]'
            '<img src="https://s3.example/inline.webp" width="1280" height="721" alt="A" /> '
            "Crédito da imagem: Autor. Uso com crédito.[/caption]"
            "<p>Mais texto.</p>"
        )
        result = clean_html(html)
        self.assertIn("<p>Texto do post.</p>", result)
        self.assertIn("<p>Mais texto.</p>", result)
        self.assertIn('<img src="https://s3.example/inline.webp"', result)

    def test_wp_caption_image_uses_safe_attributes_and_optional_caption(self):
        unsafe = clean_html(
            '[caption]<img src="javascript:alert(1)" onerror="run()" alt="x"> '
            "Crédito da imagem: Autor. Uso com crédito.[/caption]"
        )
        self.assertNotIn("<img", unsafe)
        self.assertNotIn("javascript:", unsafe)
        safe = clean_html(
            '[caption]<img src="https://cdn.example/a.jpg" onerror="run()" alt="x"> '
            "Legenda opcional.[/caption]"
        )
        self.assertIn('<img src="https://cdn.example/a.jpg" alt="x" />', safe)
        self.assertNotIn("onerror", safe)
        self.assertIn("Legenda opcional.", safe)


if __name__ == "__main__":
    unittest.main()
