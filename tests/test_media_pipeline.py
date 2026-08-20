import io
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from unicornio_editor.media.converter import (
    FEATURED_HEIGHT,
    FEATURED_WIDTH,
    convert_to_webp,
    prepare_featured_webp,
)
from unicornio_editor.media.downloader import MediaDownloadError, download_image, select_reupload_source
from unicornio_editor.media.wordpress_media import upload_image


class ImageHandler(BaseHTTPRequestHandler):
    payload = b"not-an-image"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *_args):
        pass


class FakeClient:
    def __init__(self):
        self.calls = []

    def upload_media(self, path, *, filename, alt_text, title, caption=None):
        self.calls.append((Path(path), filename, alt_text, title, caption))
        return {"id": 7, "source_url": "http://wordpress.local/media/image.webp"}


class MediaPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = Image.new("RGB", (64, 64), "red")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        ImageHandler.payload = buffer.getvalue()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ImageHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/image.png"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def test_download_validates_image_and_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = download_image(self.url, Path(directory) / "source.png", max_bytes=100000)
            self.assertGreater(path.stat().st_size, 0)

    def test_download_rejects_too_small_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MediaDownloadError):
                download_image(self.url, Path(directory) / "source.png", max_bytes=2)

    def test_legacy_local_upload_uses_effective_source_for_fallback(self):
        local = "http://wordpress.dvl.to:8080/wp-content/uploads/2019/06/old.jpg"
        effective = "https://cdn.example/current.webp"
        self.assertEqual(select_reupload_source(local, effective), effective)
        with self.assertRaises(MediaDownloadError):
            select_reupload_source(local)

    def test_current_local_upload_does_not_require_fallback(self):
        current = "http://wordpress.dvl.to:8080/wp-content/uploads/2025/03/current.webp"
        self.assertEqual(select_reupload_source(current), current)

    def test_converts_to_webp(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            source.write_bytes(ImageHandler.payload)
            output = convert_to_webp(source)
            self.assertEqual(output.suffix, ".webp")
            with Image.open(output) as image:
                self.assertEqual(image.format, "WEBP")

    def test_upload_uses_local_wordpress_media_client(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "image.webp"
            source.write_bytes(b"webp")
            result = upload_image(
                FakeClient(), source,
                {
                    "source_page_url": "https://source.example/page",
                    "direct_image_url": "https://source.example/image.webp",
                    "author": "Autor",
                    "license": "CC0",
                    "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                    "captured_at": "2026-08-20T12:00:00Z",
                    "credit_text": "Crédito da imagem: Autor. Imagem de jogo. Domínio público (CC0).",
                    "alt_text": "Imagem de jogo",
                },
            )
            self.assertEqual(result["id"], 7)

    def test_prepare_featured_webp_guarantees_1200x720(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # 1920x1080 (16:9) source must be center-cropped to 5:3.
            source = directory / "source.png"
            Image.new("RGB", (1920, 1080), "blue").save(source, format="PNG")
            output = prepare_featured_webp(source)
            with Image.open(output) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (FEATURED_WIDTH, FEATURED_HEIGHT))

    def test_prepare_featured_webp_pads_narrow_sources_to_target_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # 500x1000 (portrait) source gets cover-cropped to 5:3 then upscaled.
            source = directory / "portrait.png"
            Image.new("RGB", (500, 1000), "green").save(source, format="PNG")
            output = prepare_featured_webp(source)
            with Image.open(output) as image:
                self.assertEqual(image.size, (FEATURED_WIDTH, FEATURED_HEIGHT))

    def test_download_retries_on_rate_limit_then_succeeds(self):
        responses = [429, 200]

        class FlakyHandler(ImageHandler):
            def do_GET(self):
                code = responses.pop(0)
                if code != 200:
                    self.send_response(code)
                    self.end_headers()
                    return
                super().do_GET()

        server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/image.png"
            with tempfile.TemporaryDirectory() as directory:
                path = download_image(url, Path(directory) / "source.png")
                self.assertGreater(path.stat().st_size, 0)
        finally:
            server.shutdown()
            thread.join()
        self.assertEqual(responses, [])  # both attempts were consumed


if __name__ == "__main__":
    unittest.main()
