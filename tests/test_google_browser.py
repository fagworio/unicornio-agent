from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

from unicornio_editor.media import google_browser
from unicornio_editor.media.google_browser import _save_loaded_image, cleanup_browser_artifacts, search_google_browser_images
from unicornio_editor.media.url_safety import URLSafetyError


def test_google_browser_fails_safe_when_playwright_is_unavailable():
    report = {}
    with patch.dict("sys.modules", {"playwright": None}):
        result = search_google_browser_images("Jujutsu Kaisen", report=report)

    assert result == []
    assert report["failure_kind"] == "google_browser_unavailable"
    assert "playwright" in report["error"].lower()


def test_google_browser_pairs_each_clicked_result_with_its_own_source(monkeypatch):
    pytest.importorskip("playwright")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            if not playwright.chromium.executable_path:
                pytest.skip("Chromium is not installed")
    except Exception as exc:
        pytest.skip(f"Chromium is not available: {exc}")

    html = b"""
    <html><body>
      <img class="thumb" data-key="a" width="120" height="80" src="/thumb-a.png">
      <img class="thumb" data-key="b" width="120" height="80" src="/thumb-b.png">
      <div id="dialog" role="dialog" style="display:none"></div>
      <script>
        const dialog = document.getElementById('dialog');
        for (const thumb of document.querySelectorAll('.thumb')) {
          thumb.addEventListener('click', () => {
            const key = thumb.dataset.key;
            dialog.innerHTML = `<img width="120" height="80" src="/image-${key}.png" alt="image-${key}">` +
              `<a href="/source-${key}">source-${key}</a>`;
            dialog.style.display = 'block';
          });
        }
      </script>
    </body></html>
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/search"):
                body, content_type = html, "text/html"
            elif self.path.startswith(("/image-", "/thumb-")):
                body, content_type = b"not-a-real-image", "image/png"
            else:
                body, content_type = b"<html>source</html>", "text/html"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(google_browser, "_GOOGLE_SEARCH", f"http://127.0.0.1:{server.server_port}/search?q={{}}")
    monkeypatch.setattr(google_browser, "_BROWSER_DISABLED", False)
    try:
        results = search_google_browser_images("pairing", limit=2, timeout=10, remote_url_policy="audit")
    finally:
        server.shutdown()
        server.server_close()
        google_browser.cleanup_browser_artifacts()

    assert [(row["direct_image_url"].rsplit("/", 1)[-1], row["source_page_url"].rsplit("/", 1)[-1]) for row in results] == [
        ("image-a.png", "source-a"),
        ("image-b.png", "source-b"),
    ]


def test_browser_bytes_prefer_captured_response_and_apply_url_policy(monkeypatch):
    class Response:
        ok = True
        headers = {"content-type": "image/png"}

        def body(self):
            return b"browser-response-bytes"

    class Request:
        def get(self, *_args, **_kwargs):
            raise AssertionError("the fallback request must not be used")

    class Context:
        request = Request()

    monkeypatch.setattr(google_browser, "enforce_remote_url", lambda *_args, **_kwargs: None)
    data = _save_loaded_image(
        Context(),
        {"direct_image_url": "https://cdn.example/image.png"},
        1000,
        response_cache={"https://cdn.example/image.png": Response()},
        remote_url_policy="enforce",
    )
    path = data.get("local_image_path")
    assert path and Path(path).read_bytes() == b"browser-response-bytes"
    cleanup_browser_artifacts()

    def blocked(*_args, **_kwargs):
        raise URLSafetyError("private host")

    monkeypatch.setattr(google_browser, "enforce_remote_url", blocked)
    blocked_data = _save_loaded_image(
        Context(),
        {"direct_image_url": "http://127.0.0.1/private.png"},
        1000,
        remote_url_policy="enforce",
    )
    assert blocked_data.get("pair_error") == "remote_url_blocked"
    assert "local_image_path" not in blocked_data
