import io
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from unicornio_editor.media.source_verify import verify_downloaded_against_source


def _png_bytes(color=(200, 30, 30), size=(780, 438)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class SourcePageHandler(BaseHTTPRequestHandler):
    """Serves an article page listing two gallery images + a third unrelated one."""

    images: dict[str, bytes] = {}
    page_hits = 0

    def do_GET(self):
        if self.path == "/page.html":
            SourcePageHandler.page_hits += 1
            html = (
                '<html><head><meta property="og:image" content="/img/gallery/green-lantern-1.jpg" /></head>'
                '<body><img src="/img/gallery/green-lantern-1.jpg" />'
                '<img src="/img/gallery/green-lantern-rings-1786934482.jpg" />'
                '<img src="/img/related-other-work.jpg" />'
                "</body></html>"
            )
            data = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        data = self.images.get(self.path)
        if data is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


class SourceVerifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SourcePageHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        SourcePageHandler.images = {
            "/img/gallery/green-lantern-1.jpg": _png_bytes((10, 200, 10)),
            "/img/gallery/green-lantern-rings-1786934482.jpg": _png_bytes((10, 10, 200)),
            "/img/related-other-work.jpg": _png_bytes((200, 200, 10)),
        }
        SourcePageHandler.page_hits = 0
        self._tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _downloaded(self, name, color) -> Path:
        path = Path(self._tmp_dir) / name
        path.write_bytes(_png_bytes(color))
        return path

    def test_accepts_image_listed_on_source_page(self):
        path = self._downloaded("img.png", (10, 10, 200))
        ok, reason = verify_downloaded_against_source(
            source_page_url=f"{self.base}/page.html",
            downloaded=path,
            direct_image_url=f"{self.base}/img/gallery/green-lantern-rings-1786934482.jpg",
        )
        self.assertTrue(ok, reason)

    def test_rejects_image_not_listed_on_source_page(self):
        path = self._downloaded("img.png", (99, 99, 99))
        ok, reason = verify_downloaded_against_source(
            source_page_url=f"{self.base}/page.html",
            downloaded=path,
            direct_image_url=f"{self.base}/img/gallery/soul-eater-unknown-slug.jpg",
        )
        self.assertFalse(ok)
        self.assertIn("nao consta", reason)

    def test_rejects_divergent_bytes_for_same_slug(self):
        # A URL with the right slug but WRONG bytes (the TVLine case: the CDN
        # served another work under a green-lantern slug).
        path = self._downloaded("img.png", (200, 10, 10))
        ok, reason = verify_downloaded_against_source(
            source_page_url=f"{self.base}/page.html",
            downloaded=path,
            direct_image_url=f"{self.base}/img/gallery/green-lantern-rings-1786934482.jpg",
        )
        self.assertFalse(ok)
        self.assertIn("divergente", reason)

    def test_accepts_image_without_slug_match_when_bytes_are_listed(self):
        # Same bytes as a listed image even though the URL slug differs.
        path = self._downloaded("img.png", (200, 200, 10))
        ok, reason = verify_downloaded_against_source(
            source_page_url=f"{self.base}/page.html",
            downloaded=path,
            direct_image_url=f"{self.base}/some/other/slug.jpg",
        )
        self.assertTrue(ok, reason)

    def test_fails_closed_without_source_page(self):
        path = self._downloaded("img.png", (10, 10, 200))
        ok, reason = verify_downloaded_against_source(
            source_page_url=f"{self.base}/missing-page.html",
            downloaded=path,
            direct_image_url=f"{self.base}/img/gallery/green-lantern-1.jpg",
        )
        self.assertFalse(ok)
        self.assertIn("pagina de origem", reason)

    def test_source_page_fetched_once_per_cache(self):
        path = self._downloaded("img.png", (10, 10, 200))
        cache: dict = {}
        verify_downloaded_against_source(
            source_page_url=f"{self.base}/page.html",
            downloaded=path,
            direct_image_url=f"{self.base}/img/gallery/green-lantern-1.jpg",
            cache=cache,
        )
        verify_downloaded_against_source(
            source_page_url=f"{self.base}/page.html",
            downloaded=path,
            direct_image_url=f"{self.base}/img/gallery/green-lantern-rings-1786934482.jpg",
            cache=cache,
        )
        self.assertEqual(SourcePageHandler.page_hits, 1)


if __name__ == "__main__":
    unittest.main()
