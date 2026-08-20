import unittest

from unicornio_editor.builder import BuilderError, append_canonical_footer


class BuilderTests(unittest.TestCase):
    def test_appends_canonical_cta_and_source_from_original_link(self):
        result = append_canonical_footer("<p>Notícia.</p>", "https://example.com/news/game")
        self.assertIn("Portal de <a href=\"https://prod.unicorniohater.com.br/noticias/\">Notícias!</a>", result)
        self.assertIn('Fonte: <a href="https://example.com/news/game" target="_blank" rel="nofollow noopener">Example.com</a>', result)
        self.assertEqual(result.count("Portal de"), 1)
        self.assertEqual(result.count("Fonte:"), 1)

    def test_rebuild_is_idempotent(self):
        first = append_canonical_footer("<p>Notícia.</p>", "https://example.com/news")
        second = append_canonical_footer(first, "https://example.com/news")
        self.assertEqual(first, second)

    def test_rejects_non_http_original_link(self):
        with self.assertRaises(BuilderError):
            append_canonical_footer("<p>Notícia.</p>", "javascript:alert(1)")

    def test_rejects_missing_original_link(self):
        with self.assertRaises(BuilderError):
            append_canonical_footer("<p>Notícia.</p>", "")


if __name__ == "__main__":
    unittest.main()
