"""Testes do modulo de descoberta de imagens (media/search.py)."""

import unittest
from unittest import mock

from unicornio_editor.media.relevance import image_is_relevant
from unicornio_editor.media.search import build_bing_url, build_search_url, search_bing_images, search_web_images, search_yandex_images


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


    def test_build_bing_url_applies_size_filter(self):
        url = build_bing_url("dragon ball", size="xga")
        self.assertIn("bing.com/images/search", url)
        self.assertIn("filterui:imagesize-custom_1024_768", url)

    def test_search_bing_images_parses_purl_and_murl(self):
        html = (
            '<div>&quot;murl&quot;:&quot;https://cdn.example/dragon.jpg&quot;,'
            '&quot;turl&quot;:&quot;https://t.example/dragon_t.jpg&quot;,'
            '&quot;purl&quot;:&quot;https://news.example/dragon/&quot;</div>'
        )
        class FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self, *a):
                return html.encode()
        with mock.patch("unicornio_editor.media.search.urlopen", return_value=FakeResp()):
            results = search_bing_images("dragon ball", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["direct_image_url"], "https://cdn.example/dragon.jpg")
        self.assertEqual(results[0]["source_page_url"], "https://news.example/dragon/")

    def test_search_web_images_rotates_to_bing(self):
        bing_html = (
            '&quot;murl&quot;:&quot;https://cdn.example/a.jpg&quot;,'
            '&quot;turl&quot;:&quot;https://t.example/a_t.jpg&quot;,'
            '&quot;purl&quot;:&quot;https://news.example/a/&quot;'
        )
        class FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self, *a):
                return bing_html.encode()
        with mock.patch("unicornio_editor.media.search.urlopen", return_value=FakeResp()):
            results = search_web_images("dragon ball", limit=3)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].get("engine"), "bing")
        self.assertEqual(results[0]["direct_image_url"], "https://cdn.example/a.jpg")


    def test_search_yandex_images_parses_img_url_param(self):
        # O Yandex embute a URL real no param img_url= (URL-encoded) dos itens.
        html = (
            '<a href="/images/search?from=tabbar&img_url=https%3A%2F%2Fi.pinimg.com%2Fx.jpg'
            '&pos=0&rpt=simage&text=dragon+ball">'
        )
        class FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self, *a):
                return html.encode()
        with mock.patch("unicornio_editor.media.search.urlopen", return_value=FakeResp()):
            results = search_yandex_images("dragon ball", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["direct_image_url"], "https://i.pinimg.com/x.jpg")

    def test_search_web_images_alternates_primary_engine_by_query(self):
        # HTML que AMBAS as engines conseguem parsear: a primaria (escolhida
        # pelo hash da query) vence. Prova a rotacao real, nao so o failover.
        import zlib

        html = (
            '&quot;murl&quot;:&quot;https://cdn.example/bing.jpg&quot;,'
            '&quot;turl&quot;:&quot;https://t.example/bing_t.jpg&quot;,'
            '&quot;purl&quot;:&quot;https://news.example/bing/&quot; '
            '<a href="/images/search?img_url=https%3A%2F%2Fi.pinimg.com%2Fyandex.jpg&pos=0">'
        )

        def pick_query(want_yandex):
            i = 0
            while True:
                q = f"alternancia teste {i}"
                if bool(zlib.crc32(q.encode("utf-8")) & 1) == want_yandex:
                    return q
                i += 1

        class FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self, *a):
                return html.encode()

        with mock.patch("unicornio_editor.media.search.urlopen", return_value=FakeResp()):
            q_bing = pick_query(want_yandex=False)
            results = search_web_images(q_bing, limit=3)
            self.assertEqual(results[0]["engine"], "bing")
            self.assertEqual(results[0]["direct_image_url"], "https://cdn.example/bing.jpg")

            q_yandex = pick_query(want_yandex=True)
            results = search_web_images(q_yandex, limit=3)
            self.assertEqual(results[0]["engine"], "yandex")
            self.assertEqual(results[0]["direct_image_url"], "https://i.pinimg.com/yandex.jpg")

            # Determinismo: a MESMA query sempre comeca na mesma engine.
            results = search_web_images(q_bing, limit=3)
            self.assertEqual(results[0]["engine"], "bing")
            results = search_web_images(q_yandex, limit=3)
            self.assertEqual(results[0]["engine"], "yandex")


if __name__ == "__main__":
    unittest.main()
