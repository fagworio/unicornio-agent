import unittest

from unicornio_editor.builder import BuilderError, append_canonical_footer


class BuilderTests(unittest.TestCase):
    def test_appends_canonical_cta_and_source_from_original_link(self):
        result = append_canonical_footer("<p>Notícia.</p>", "https://example.com/news/game")
        self.assertIn("Portal de <a href=\"https://prod.unicorniohater.com.br/noticias/\">Notícias!</a>", result)
        self.assertIn(
            'Fonte: <a href="https://example.com/news/game" target="_blank" rel="nofollow noopener">Example</a>.</em>',
            result,
        )
        self.assertEqual(result.count("Portal de"), 1)
        self.assertEqual(result.count("Fonte:"), 1)

    def test_rebuild_is_idempotent(self):
        first = append_canonical_footer("<p>Notícia.</p>", "https://example.com/news")
        second = append_canonical_footer(first, "https://example.com/news")
        self.assertEqual(first, second)

    def test_source_uses_canonical_period_inside_em(self):
        result = append_canonical_footer("<p>Notícia.</p>", "https://www.cinelinx.com/games/game-news/item")
        self.assertIn(
            '<em>Fonte: <a href="https://www.cinelinx.com/games/game-news/item" '
            'target="_blank" rel="nofollow noopener">Cinelinx</a>.</em>',
            result,
        )

    def test_cta_is_kept_without_original_link(self):
        result = append_canonical_footer("<p>Notícia.</p>", None)
        self.assertIn("Portal de <a href=\"https://prod.unicorniohater.com.br/noticias/\">Notícias!</a>", result)
        self.assertNotIn("Fonte:", result)

    def test_cta_removes_legacy_source_without_original_link(self):
        legacy = '<p>Notícia.</p><hr /><a href="https://old.example"><strong><em>Fonte</em></strong></a>'
        result = append_canonical_footer(legacy, None)
        self.assertIn("Portal de", result)
        self.assertNotIn("old.example", result)
        self.assertNotIn("Fonte", result)

    def test_rejects_non_http_original_link(self):
        with self.assertRaises(BuilderError):
            append_canonical_footer("<p>Notícia.</p>", "javascript:alert(1)")



if __name__ == "__main__":
    unittest.main()
