import io
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from unicornio_editor.media.converter import convert_to_webp
from unicornio_editor.media.downloader import MediaDownloadError, download_image
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


if __name__ == "__main__":
    unittest.main()
