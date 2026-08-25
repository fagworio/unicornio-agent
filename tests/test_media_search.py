"""Testes do modulo de descoberta de imagens (media/search.py)."""

import unittest
from unittest import mock

from unicornio_editor.media.relevance import image_is_relevant
from unicornio_editor.media.search import build_search_url, search_web_images


class SearchUrlTests(unittest.TestCase):
    def test_build_search_url_applies_filters(self):
        url = build_search_url("redfall xbox series")
        self.assertIn("imgsz=xga", url)
        self.assertIn("imgar=w", url)
        self.assertIn("udm=2", url)
        self.assertIn("as_q=redfall+xbox+series", url)

    def test_build_search_url_invalid_values_fall_back(self):
        url = build_search_url("x", size="ZZZ", ratio="ZZZ")
        self.assertIn("imgsz=xga", url)
        self.assertIn("imgar=w", url)

    def test_custom_size_and_ratio_are_honored(self):
        url = build_search_url("x", size="qhd", ratio="x")
        self.assertIn("imgsz=qhd", url)
        self.assertIn("imgar=x", url)


class SearchWebImagesTests(unittest.TestCase):
    def test_empty_query_returns_empty(self):
        self.assertEqual(search_web_images("   "), [])

    def test_unreachable_search_returns_empty_gracefully(self):
        with mock.patch(
            "unicornio_editor.media.search.urlopen",
            side_effect=OSError("no network"),
        ):
            self.assertEqual(search_web_images("redfall"), [])

    def test_parses_candidates_and_drops_google_previews(self):
        html = (
            '<html><body><script>var data = '
            '{"1": {"tu": "https://thumb.example/t.jpg", '
            '"ou": "https://cdn.example/redfall-header.jpg", '
            '"ru": "https://news.example/redfall/", '
            '"pt": "Redfall key art"}, '
            '"2": {"tu": "https://encrypted-tbn0.gstatic.com/x", '
            '"ou": "https://lh3.googleusercontent.com/x"}}'
            "</script></body></html>"
        )
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *args):
                return html.encode()

        with mock.patch(
            "unicornio_editor.media.search.urlopen",
            return_value=FakeResp(),
        ):
            results = search_web_images("redfall xbox", limit=10)
        # Apenas o candidato com URL de imagem real; o preview do Google e
        # descartado (lh3/encrypted-tbn/gstatic nao sao fonte).
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["direct_image_url"], "https://cdn.example/redfall-header.jpg")
        self.assertEqual(results[0]["source_page_url"], "https://news.example/redfall/")
        self.assertEqual(results[0]["query"], "redfall xbox")
        self.assertEqual(results[0]["size_filter"], "1024x768|w")


class SearchQueryEvidenceTests(unittest.TestCase):
    def test_search_query_counts_as_evidence_for_featured(self):
        # Featured: filename generico mas query de descoberta carrega a obra.
        relevant = image_is_relevant(
            alt_text="",
            credit_text="",
            source_url="https://cdn.example/header.jpg",
            search_query="redfall xbox series",
            entities={"redfall"},
            source_only=True,
        )
        self.assertTrue(relevant)

    def test_search_query_counts_for_inline(self):
        relevant = image_is_relevant(
            alt_text="",
            credit_text="",
            source_url="https://cdn.example/img.jpg",
            search_query="netflix series",
            entities={"netflix"},
        )
        self.assertTrue(relevant)

    def test_query_alone_does_not_override_absent_source_url(self):
        # Sem URL de origem, so a query nao basta (fail-closed).
        relevant = image_is_relevant(
            alt_text="",
            credit_text="",
            source_url="",
            search_query="redfall",
            entities={"redfall"},
            source_only=True,
        )
        self.assertFalse(relevant)

    def test_wrong_query_does_not_pass(self):
        relevant = image_is_relevant(
            alt_text="",
            credit_text="",
            source_url="https://cdn.example/castle.jpg",
            search_query="disney castle",
            entities={"redfall"},
            source_only=True,
        )
        self.assertFalse(relevant)


if __name__ == "__main__":
    unittest.main()
