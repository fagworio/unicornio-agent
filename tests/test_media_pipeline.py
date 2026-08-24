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
    MediaConversionError,
    convert_to_webp,
    image_has_transparency,
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
            big = io.BytesIO()
            Image.new("RGB", (800, 600), "blue").save(big, format="PNG")
            source.write_bytes(big.getvalue())
            output = convert_to_webp(source)
            self.assertEqual(output.suffix, ".webp")
            with Image.open(output) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (800, 600))

    def test_image_has_transparency_detects_alpha(self):
        with tempfile.TemporaryDirectory() as directory:
            rgb = Path(directory) / "rgb.png"
            Image.new("RGB", (64, 64), "red").save(rgb, format="PNG")
            self.assertFalse(image_has_transparency(rgb))
            opaque = Path(directory) / "opaque.png"
            Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(opaque, format="PNG")
            self.assertFalse(image_has_transparency(opaque))  # alpha 255 em tudo
            alpha = Path(directory) / "alpha.png"
            half = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            half.paste(Image.new("RGBA", (32, 64), (255, 0, 0, 255)), (32, 0))
            half.save(alpha, format="PNG")
            self.assertTrue(image_has_transparency(alpha))

    def test_convert_to_webp_flattens_transparency(self):
        # Politica de transparencia: fonte com canal alpha NAO entra no post —
        # o WebP publicado e composto sobre fundo branco (opaco), nunca RGBA.
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "alpha.png"
            half = Image.new("RGBA", (800, 600), (0, 0, 0, 0))  # metade transparente
            half.paste(Image.new("RGBA", (400, 600), (200, 50, 50, 255)), (400, 0))
            half.save(source, format="PNG")
            output = convert_to_webp(source)
            with Image.open(output) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.mode, "RGB")  # sem canal alpha
                white = image.getpixel((0, 300))  # era transparente -> branco
                assert isinstance(white, tuple) and len(white) == 3
                self.assertGreaterEqual(white[0], 240)
                self.assertGreaterEqual(white[1], 240)
                self.assertGreaterEqual(white[2], 240)
                red = image.getpixel((600, 300))  # era opaco -> mantem a cor
                assert isinstance(red, tuple) and len(red) == 3
                self.assertGreater(red[0], 150)
                self.assertLess(red[1], 100)

    def test_rejects_fully_transparent_image(self):
        # Fail-closed: imagem sem nenhum pixel opaco e rejeitada — achatar
        # sobre branco publicaria um quadro vazio.
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "empty.png"
            Image.new("RGBA", (800, 600), (0, 0, 0, 0)).save(source, format="PNG")
            with self.assertRaises(MediaConversionError):
                convert_to_webp(source)
            self.assertFalse(Path(str(source).replace(".png", ".webp")).exists())

    def test_prepare_featured_webp_flattens_transparency(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "keyart.png"
            half = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
            half.paste(Image.new("RGBA", (1920, 540), (30, 120, 200, 255)), (0, 540))
            half.save(source, format="PNG")
            output = prepare_featured_webp(source)
            with Image.open(output) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.size, (FEATURED_WIDTH, FEATURED_HEIGHT))
                top = image.getpixel((FEATURED_WIDTH // 2, 100))  # era transparente
                assert isinstance(top, tuple) and len(top) == 3
                self.assertGreaterEqual(top[0], 240)
                self.assertGreaterEqual(top[1], 240)
                self.assertGreaterEqual(top[2], 240)

    def test_rejects_small_inline_source_below_minimum_width(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            small = io.BytesIO()
            Image.new("RGB", (500, 300), "green").save(small, format="PNG")
            source.write_bytes(small.getvalue())
            with self.assertRaises(MediaConversionError):
                convert_to_webp(source)

    def test_caps_inline_width_at_maximum(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            wide = io.BytesIO()
            Image.new("RGB", (2000, 1125), "red").save(wide, format="PNG")
            source.write_bytes(wide.getvalue())
            output = convert_to_webp(source)
            with Image.open(output) as image:
                self.assertEqual(image.size, (1280, 720))

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

    def test_prepare_featured_webp_guarantees_1280x720(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # 1920x1080 (16:9) source must be center-cropped to 16:9.
            source = directory / "source.png"
            Image.new("RGB", (1920, 1080), "blue").save(source, format="PNG")
            output = prepare_featured_webp(source)
            with Image.open(output) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (FEATURED_WIDTH, FEATURED_HEIGHT))

    def test_inline_width_capped_at_1280(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # Posters wider than 1280px are scaled down to 1280px wide
            # (aspect kept) so pages do not ship oversized images.
            source = directory / "poster.png"
            Image.new("RGB", (4000, 2000), "red").save(source, format="PNG")
            output = convert_to_webp(source)
            with Image.open(output) as image:
                self.assertEqual(image.size, (1280, 640))

    def test_inline_smaller_than_1280_is_never_upscaled(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "small.png"
            Image.new("RGB", (800, 600), "red").save(source, format="PNG")
            output = convert_to_webp(source)
            with Image.open(output) as image:
                self.assertEqual(image.size, (800, 600))

    def test_prepare_featured_webp_rejects_portrait_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # A portrait source cannot become a sane 5:3 featured image:
            # cover-cropping destroys more than half the frame, so it must be
            # rejected and the agent must pick a landscape key art instead.
            source = directory / "portrait.png"
            Image.new("RGB", (500, 1000), "green").save(source, format="PNG")
            with self.assertRaises(MediaConversionError):
                prepare_featured_webp(source)
            self.assertFalse(Path(str(source).replace(".png", "_featured.webp")).exists())

    def test_exif_orientation_is_applied_before_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # Phone/camera photos store rotation in EXIF (Orientation=6) with
            # the raw pixels lying sideways. The pipeline must transpose the
            # image before converting, or the photo is published lying down.
            raw = Image.new("RGB", (4000, 3000), "purple")
            exif = Image.Exif()
            exif[274] = 6
            source = directory / "sideways.jpg"
            raw.save(source, format="JPEG", exif=exif, quality=90)
            self.assertEqual(Image.open(source).size, (4000, 3000))  # raw pixels
            output = convert_to_webp(source)
            with Image.open(output) as image:
                self.assertEqual(image.format, "WEBP")
                # Transposed to portrait (3000x4000), then inline width capped
                # at 1280px: still portrait, never lying down.
                self.assertEqual(image.size, (1280, 1707))

    def test_prepare_featured_webp_applies_exif_then_keeps_landscape(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # A photo whose raw pixels are portrait but EXIF says landscape
            # (Orientation=5) becomes landscape after transpose and is valid.
            raw = Image.new("RGB", (3000, 4000), "orange")
            exif = Image.Exif()
            exif[274] = 5  # rotate 90 CCW -> 4000x3000 landscape
            source = directory / "exif_portrait_raw.jpg"
            raw.save(source, format="JPEG", exif=exif, quality=90)
            output = prepare_featured_webp(source)
            with Image.open(output) as image:
                self.assertEqual(image.format, "WEBP")
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
