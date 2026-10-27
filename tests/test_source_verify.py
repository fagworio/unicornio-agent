import io
import json
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from unicornio_editor.media.source_verify import (
    _normalized_url,
    validate_discovered_candidate,
    verify_downloaded_against_source,
    enrich_wikimedia_rights,
    search_wikimedia_commons_candidates,
)


def _png_bytes(color=(200, 30, 30), size=(780, 438)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class SourcePageHandler(BaseHTTPRequestHandler):
    """Serves an article page listing two gallery images + a third unrelated one."""

    images: dict[str, bytes] = {}
    page_html: dict[str, str] = {}
    page_hits = 0

    def do_GET(self):
        if self.path in SourcePageHandler.page_html:
            SourcePageHandler.page_hits += 1
            data = SourcePageHandler.page_html[self.path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
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
        data = self.images.get(self.path) or self.images.get(self.path.split("?", 1)[0])
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
    def test_normalized_url_encodes_spaces_without_changing_source_identity(self):
        self.assertEqual(
            _normalized_url("https://cdn.test/art work.jpg"),
            "https://cdn.test/art%20work.jpg",
        )
        self.assertEqual(
            _normalized_url("https://cdn.test/art%20work.jpg"),
            _normalized_url("https://cdn.test/art work.jpg"),
        )

    def test_relative_source_page_is_rejected_at_the_boundary(self):
        result = validate_discovered_candidate(
            {
                "direct_image_url": "https://cdn.test/art.jpg",
                "source_page_url": "/search/coastal landscape/",
            },
            subject="coastal landscape",
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason_code"], "source_page_unavailable")

    def test_absolute_source_page_with_unescaped_space_is_candidate_failure(self):
        result = validate_discovered_candidate(
            {
                "direct_image_url": "https://cdn.test/art.jpg",
                "source_page_url": "https://example.test/search/coastal landscape/",
            },
            subject="coastal landscape",
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason_code"], "source_page_unavailable")
        self.assertIn("invalid", result["reason"])

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
        SourcePageHandler.page_html = {}
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

    def test_accepts_recompressed_same_frame_as_visual_variant(self):
        # CDN can return JPEG/WebP bytes different from the listed PNG while
        # preserving the same pixels/frame. Provenance must not reject this.
        original = Image.new("RGB", (780, 438), (10, 10, 200))
        listed = io.BytesIO()
        original.save(listed, format="JPEG", quality=92)
        SourcePageHandler.images["/img/gallery/green-lantern-rings-1786934482.jpg"] = listed.getvalue()
        downloaded = Path(self._tmp_dir) / "recompressed.webp"
        original.save(downloaded, format="WEBP", quality=72)
        ok, reason = verify_downloaded_against_source(
            source_page_url=f"{self.base}/page.html",
            downloaded=downloaded,
            direct_image_url=f"{self.base}/img/gallery/green-lantern-rings-1786934482.jpg",
        )
        self.assertTrue(ok, reason)
        self.assertIn("visual", reason)

    def test_accepts_cdn_rendition_declared_only_in_lazy_picture_srcset(self):
        """A CDN resize is verified by bytes, never accepted by its name alone."""
        frame = _png_bytes((30, 90, 190), size=(1600, 900))
        SourcePageHandler.page_html["/dynamic.html"] = (
            '<article><picture><source data-srcset="'
            '/cdn/star-fox-battle-1600x900.jpg?width=1600 1600w">'
            '<img data-src="/cdn/star-fox-battle-1600x900.jpg?width=1600"></picture></article>'
        )
        SourcePageHandler.images["/cdn/star-fox-battle-1600x900.jpg"] = frame
        SourcePageHandler.images["/cdn/star-fox-battle-768x432.webp"] = frame
        downloaded = Path(self._tmp_dir) / "star-fox-battle-768x432.webp"
        downloaded.write_bytes(frame)

        preflight = validate_discovered_candidate({
            "direct_image_url": f"{self.base}/cdn/star-fox-battle-768x432.webp?format=webp",
            "source_page_url": f"{self.base}/dynamic.html",
        })
        self.assertTrue(preflight["valid"], preflight["reason"])
        self.assertEqual(preflight["verification_level"], "PIXEL_IDENTICAL")

        ok, reason = verify_downloaded_against_source(
            source_page_url=f"{self.base}/dynamic.html",
            downloaded=downloaded,
            direct_image_url=f"{self.base}/cdn/star-fox-battle-768x432.webp?format=webp",
        )

        self.assertTrue(ok, reason)
        self.assertIn("rendicao CDN", reason)

    def test_resolve_visual_yandex_promove_url_canonica_da_pagina(self):
        SourcePageHandler.images["/discovery/yandex-copy.jpg"] = _png_bytes((10, 10, 200))
        out = validate_discovered_candidate(
            {
                "direct_image_url": f"{self.base}/discovery/yandex-copy.jpg",
                "source_page_url": f"{self.base}/page.html",
            }
        )
        self.assertTrue(out["valid"], out["reason"])
        self.assertEqual(
            out["matched_image_url"],
            f"{self.base}/img/gallery/green-lantern-rings-1786934482.jpg",
        )
        self.assertEqual(out["verification_level"], "PIXEL_IDENTICAL")
        self.assertTrue(out["source_comparison_proved"])

    def test_html_reference_match_does_not_claim_byte_comparison(self):
        image_url = f"{self.base}/img/gallery/green-lantern-rings-1786934482.jpg"
        out = validate_discovered_candidate({
            "direct_image_url": image_url,
            "source_page_url": f"{self.base}/page.html",
        })
        self.assertTrue(out["valid"], out["reason"])
        self.assertEqual(out["verification_level"], "EXACT_URL")
        self.assertFalse(out["source_comparison_proved"])

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

    def test_exact_image_after_first_twelve_is_still_verified(self):
        target = _png_bytes((40, 50, 60))
        SourcePageHandler.images["/img/gallery/late-target.jpg"] = target
        original_get = SourcePageHandler.do_GET

        def do_get(handler):
            if handler.path == "/many.html":
                decoys = "".join(
                    f'<img src="/img/decoy-{index}.jpg" />' for index in range(12)
                )
                html = f"<html><body>{decoys}<img src=\"/img/gallery/late-target.jpg\" /></body></html>"
                data = html.encode()
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)
                return
            original_get(handler)

        path = Path(self._tmp_dir) / "late.png"
        path.write_bytes(target)
        with mock.patch.object(SourcePageHandler, "do_GET", do_get):
            ok, reason = verify_downloaded_against_source(
                source_page_url=f"{self.base}/many.html",
                downloaded=path,
                direct_image_url=f"{self.base}/img/gallery/late-target.jpg",
            )
        self.assertTrue(ok, reason)
        self.assertIn("URL exata", reason)

    def test_exact_reference_timeout_is_inconclusive_not_bytes_mismatch(self):
        path = self._downloaded("img.png", (10, 10, 200))
        image_url = f"{self.base}/img/gallery/green-lantern-rings-1786934482.jpg"
        with mock.patch(
            "unicornio_editor.media.source_verify._fetch", return_value=None
        ):
            ok, reason = verify_downloaded_against_source(
                source_page_url=f"{self.base}/page.html",
                downloaded=path,
                direct_image_url=image_url,
                cache={f"{self.base}/page.html": [image_url]},
            )
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("source_asset_unavailable:"), reason)
        self.assertNotIn("source_bytes_mismatch", reason)

    def test_html_reference_is_decode_failure_not_source_mismatch(self):
        path = self._downloaded("img.png", (10, 10, 200))
        image_url = f"{self.base}/img/gallery/green-lantern-rings-1786934482.jpg"
        with mock.patch(
            "unicornio_editor.media.source_verify._fetch",
            return_value=b"<html>not an image</html>",
        ):
            ok, reason = verify_downloaded_against_source(
                source_page_url=f"{self.base}/page.html",
                downloaded=path,
                direct_image_url=image_url,
                cache={f"{self.base}/page.html": [image_url]},
            )
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("source_decode_failed:"), reason)

    def test_limited_scan_is_incomplete_not_absence_proof(self):
        path = self._downloaded("img.png", (10, 10, 200))
        listed = [f"{self.base}/img/decoy-{index}.jpg" for index in range(8)]
        with mock.patch(
            "unicornio_editor.media.source_verify._fetch",
            return_value=_png_bytes((200, 10, 10)),
        ):
            ok, reason = verify_downloaded_against_source(
                source_page_url=f"{self.base}/page.html",
                downloaded=path,
                direct_image_url=f"{self.base}/img/target.jpg",
                cache={f"{self.base}/page.html": listed},
            )
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("source_scan_incomplete:"), reason)

    def test_partial_variant_comparison_is_not_bytes_mismatch(self):
        path = self._downloaded("img.png", (10, 10, 200))
        listed = [
            f"{self.base}/img/gallery/hero.jpg?variant={index}"
            for index in range(8)
        ]
        divergent = _png_bytes((200, 10, 10))
        with mock.patch(
            "unicornio_editor.media.source_verify._fetch",
            side_effect=[divergent, None, divergent, divergent, divergent, divergent],
        ):
            ok, reason = verify_downloaded_against_source(
                source_page_url=f"{self.base}/page.html",
                downloaded=path,
                direct_image_url=f"{self.base}/discovery/hero.jpg",
                cache={f"{self.base}/page.html": listed},
            )
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("source_scan_incomplete:"), reason)
        self.assertNotIn("source_bytes_mismatch", reason)

    def test_wikimedia_api_rights_enrichment_binds_exact_file(self):
        payload = json.dumps({"query": {"pages": {"1": {"imageinfo": [{
            "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Game.jpg",
            "extmetadata": {
                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
                "Artist": {"value": "<a href=\"https://commons.wikimedia.org/wiki/User:Author\">Author</a>"},
            },
        }]}}}}).encode()
        candidate = {
            "source_page_url": "https://commons.wikimedia.org/wiki/File:Game.jpg",
            "direct_image_url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Game.jpg",
            "subject": "Game",
        }
        with mock.patch("unicornio_editor.media.source_verify._fetch", return_value=payload):
            enriched = enrich_wikimedia_rights(candidate)
        self.assertTrue(enriched["rights_verified"])
        self.assertEqual(enriched["direct_image_url"], "https://upload.wikimedia.org/wikipedia/commons/a/ab/Game.jpg")
        self.assertEqual(enriched["license"], "CC BY-SA 4.0")
        self.assertEqual(enriched["source_rights_attestation"]["provider"], "wikimedia_commons_api")

    def test_wikimedia_search_returns_only_attested_licensed_files(self):
        payload = json.dumps({"query": {"pages": {
            "1": {"title": "File:Game.jpg", "imageinfo": [{
                "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Game.jpg",
                "extmetadata": {
                    "LicenseShortName": {"value": "CC BY-SA 4.0"},
                    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
                    "Artist": {"value": "Author"},
                },
            }]},
            "2": {"title": "File:NoRights.jpg", "imageinfo": [{
                "url": "https://upload.wikimedia.org/no-rights.jpg",
                "extmetadata": {},
            }]},
        }}}).encode()
        with mock.patch("unicornio_editor.media.source_verify._fetch", return_value=payload), mock.patch(
            "unicornio_editor.media.source_verify._validate_discovered_candidate",
            return_value={"valid": True, "source_verified": True, "reason": "source_reference_found: test"},
        ):
            candidates = search_wikimedia_commons_candidates("Game", limit=8)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["rights_verified"])
        self.assertEqual(candidates[0]["source_rights_attestation"]["provider"], "wikimedia_commons_api")
        self.assertEqual(candidates[0]["evidence"]["verdict"], "deterministic_match")

    def test_wikimedia_api_rejects_different_discovered_representation(self):
        payload = json.dumps({"query": {"pages": {"1": {"imageinfo": [{
            "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Game.jpg",
            "extmetadata": {
                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
                "Artist": {"value": "Author"},
            },
        }]}}}}).encode()
        candidate = {
            "source_page_url": "https://commons.wikimedia.org/wiki/File:Game.jpg",
            "direct_image_url": "https://upload.wikimedia.org/discovered.jpg",
            "subject": "Game",
        }
        with mock.patch(
            "unicornio_editor.media.source_verify._fetch",
            side_effect=[payload, _png_bytes((200, 30, 30)), _png_bytes((30, 30, 200))],
        ):
            enriched = enrich_wikimedia_rights(candidate)
        self.assertIs(enriched["rights_verified"], False)
        self.assertEqual(enriched["rights_decision"]["status"], "TECHNICAL_FAILURE")
        self.assertTrue(enriched["rights_decision"]["retryable"])


if __name__ == "__main__":
    unittest.main()
