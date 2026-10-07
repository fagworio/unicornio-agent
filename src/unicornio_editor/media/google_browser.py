"""Google Images discovery through a real browser session.

The browser is an optional discovery provider. It never becomes the source of
truth: every candidate still needs a non-Google ``source_page_url`` and must
pass the existing provenance/relevance gates. When Playwright is unavailable,
Google blocks the session, or its DOM changes, this module fails closed and
lets the normal Bing/Yandex fallback continue.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse


_GOOGLE_SEARCH = "https://www.google.com/search?tbm=isch&q={}"
_GOOGLE_HOST_MARKERS = (
    "google.",
    "googleusercontent.",
    "gstatic.com",
)
_BLOCK_MARKERS = (
    "unusual traffic",
    "/sorry/",
    "recaptcha",
    "captcha",
    "consent.google",
)
_IMAGE_EXT = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|bmp)(?:[?#]|$)", re.I)


def _is_external_http(value: str) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and bool(host) and not any(
        marker in host for marker in _GOOGLE_HOST_MARKERS
    )


def _report(report: dict[str, Any] | None, *, kind: str, error: str = "", objects: int = 0, candidates: int = 0) -> None:
    if report is None:
        return
    report.update({
        "http_status": int(report.get("http_status") or 0),
        "html_bytes": int(report.get("html_bytes") or 0),
        "objects_parsed": int(objects),
        "candidates": int(candidates),
        "failure_kind": kind,
        "error": error[:240],
        "parser_version": 1,
    })


def _candidate(query: str, data: dict[str, Any]) -> dict[str, Any]:
    direct = str(data.get("direct_image_url") or "").strip()
    source = str(data.get("source_page_url") or "").strip()
    candidate_id = hashlib.sha256(
        f"google_browser|{query}|{direct}".encode("utf-8", "ignore")
    ).hexdigest()[:20]
    result: dict[str, Any] = {
        "candidate_id": candidate_id,
        "query": query,
        "engine": "google_browser",
        "title": str(data.get("title") or "")[:200],
        "thumbnail_url": str(data.get("thumbnail_url") or ""),
        "direct_image_url": direct,
        "discovery_image_url": direct,
        "source_page_url": source,
        "usable": bool(_is_external_http(direct) and _is_external_http(source)),
        "discovery_only": not bool(_is_external_http(direct) and _is_external_http(source)),
        "rejected_reason": "" if source else "missing_source_page",
        "width": int(data.get("width") or 0),
        "height": int(data.get("height") or 0),
    }
    local_path = str(data.get("local_image_path") or "")
    if local_path:
        result["local_image_path"] = local_path
    for key in ("sha256", "mime"):
        if data.get(key):
            result[key] = str(data[key])
    return result


def _extract_open_result(page) -> dict[str, Any]:
    """Extract the opened Google result without relying on one CSS class."""
    return page.evaluate(
        """
        () => {
          const bad = /(^|\\.)google\\.|googleusercontent\\.|gstatic\\.com/i;
          const images = [...document.images]
            .map(img => ({
              src: img.currentSrc || img.src || '',
              alt: img.alt || '',
              width: img.naturalWidth || img.width || 0,
              height: img.naturalHeight || img.height || 0
            }))
            .filter(item => /^https?:/i.test(item.src) && !bad.test(item.src))
            .sort((a, b) => (b.width * b.height) - (a.width * a.height));
          const anchors = [...document.querySelectorAll('a[href]')]
            .map(a => ({href: a.href || '', text: (a.innerText || '').trim()}))
            .filter(item => /^https?:/i.test(item.href) && !bad.test(item.href));
          const image = images[0] || {};
          const source = anchors.find(item => !/\\.(jpe?g|png|webp|gif|avif|bmp)([?#]|$)/i.test(item.href));
          return {
            direct_image_url: image.src || '',
            thumbnail_url: '',
            source_page_url: source ? source.href : '',
            title: image.alt || (source ? source.text : ''),
            width: image.width || 0,
            height: image.height || 0
          };
        }
        """
    ) or {}


def _save_loaded_image(context, data: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    """Persist the browser-loaded bytes for the current process only."""
    direct = str(data.get("direct_image_url") or "")
    request = getattr(context, "request", None)
    if not direct or request is None:
        return data
    try:
        response = request.get(direct, timeout=timeout_ms, fail_on_status_code=False)
        if not getattr(response, "ok", False):
            return data
        body = response.body()
        if not body or len(body) > 8 * 1024 * 1024:
            return data
        mime = str((getattr(response, "headers", {}) or {}).get("content-type") or "").split(";", 1)[0]
        suffix = ".bin"
        if mime.startswith("image/"):
            suffix = "." + mime.split("/", 1)[1].replace("jpeg", "jpg")
        handle = tempfile.NamedTemporaryFile(prefix="unicornio-google-browser-", suffix=suffix, delete=False)
        try:
            handle.write(body)
        finally:
            handle.close()
        data["local_image_path"] = handle.name
        data["sha256"] = hashlib.sha256(body).hexdigest()
        data["mime"] = mime
    except Exception:  # noqa: BLE001 - browser bytes are an optimization
        return data
    return data


def search_google_browser_images(
    query: str,
    *,
    size: str = "xga",
    ratio: str = "w",
    limit: int = 10,
    timeout: float = 30.0,
    report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Discover Google Images results through Playwright, fail-closed."""
    query = str(query or "").strip()
    if not query:
        return []
    if os.environ.get("EDITOR_GOOGLE_BROWSER_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        _report(report, kind="google_browser_unavailable", error="provider disabled")
        return []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - optional dependency
        _report(report, kind="google_browser_unavailable", error=f"playwright unavailable: {exc}")
        return []

    timeout_ms = max(1000, int(float(timeout) * 1000))
    results: list[dict[str, Any]] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 1000},
            )
            page = context.new_page()
            page.goto(_GOOGLE_SEARCH.format(quote_plus(query)), wait_until="domcontentloaded", timeout=timeout_ms)
            body = (page.content() or "")[:500_000]
            lowered = body.casefold()
            if any(marker in lowered or marker in str(page.url).casefold() for marker in _BLOCK_MARKERS):
                _report(report, kind="google_browser_unavailable", error="captcha/consent/interstitial")
                browser.close()
                return []
            thumbnails = page.locator("img")
            count = min(int(thumbnails.count()), max(1, int(limit)) * 4)
            seen: set[str] = set()
            for index in range(count):
                try:
                    thumbnails.nth(index).click(timeout=min(timeout_ms, 3000), no_wait_after=True)
                    page.wait_for_timeout(120)
                    data = _extract_open_result(page)
                except Exception:
                    continue
                direct = str(data.get("direct_image_url") or "")
                source = str(data.get("source_page_url") or "")
                if not _is_external_http(direct) or direct in seen:
                    continue
                seen.add(direct)
                if source:
                    data = _save_loaded_image(context, data, timeout_ms)
                results.append(_candidate(query, data))
                if len(results) >= int(limit):
                    break
            browser.close()
    except Exception as exc:  # noqa: BLE001 - DOM/browser changes are fail-safe
        _report(report, kind="google_browser_unavailable", error=f"{type(exc).__name__}: {exc}", candidates=len(results))
        return results
    _report(report, kind="ok" if results else "google_browser_unavailable", objects=count if 'count' in locals() else 0, candidates=len(results))
    return results


__all__ = ["search_google_browser_images"]
