from unittest.mock import patch

from unicornio_editor.media.google_browser import search_google_browser_images


def test_google_browser_fails_safe_when_playwright_is_unavailable():
    report = {}
    with patch.dict("sys.modules", {"playwright": None}):
        result = search_google_browser_images("Jujutsu Kaisen", report=report)

    assert result == []
    assert report["failure_kind"] == "google_browser_unavailable"
    assert "playwright" in report["error"].lower()
