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


if __name__ == "__main__":
    unittest.main()
