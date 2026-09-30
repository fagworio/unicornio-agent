import unittest

from unicornio_editor.media.page_assets import extract_page_assets


class PageAssetTests(unittest.TestCase):
    def test_extracts_lazy_srcset_social_jsonld_and_extensionless_cdn(self):
        html = '''
        <meta property="og:image" content="/og-image?id=1">
        <meta name="twitter:image" content="/twitter-image?id=2">
        <img data-lazy-src="/lazy/123" alt="Arte">
        <picture><source data-srcset="/hero/456?w=1200 1200w, /hero/456?w=640 640w"></picture>
        <script type="application/ld+json">{"image":{"contentUrl":"/jsonld/789"}}</script>
        '''
        assets = extract_page_assets(html, "https://example.test/article")
        urls = {asset.url for asset in assets}
        self.assertIn("https://example.test/og-image?id=1", urls)
        self.assertIn("https://example.test/twitter-image?id=2", urls)
        self.assertIn("https://example.test/lazy/123", urls)
        self.assertIn("https://example.test/hero/456?w=1200", urls)
        self.assertIn("https://example.test/jsonld/789", urls)
        self.assertEqual(next(a for a in assets if a.url.endswith("/lazy/123")).alt, "Arte")


if __name__ == "__main__":
    unittest.main()
