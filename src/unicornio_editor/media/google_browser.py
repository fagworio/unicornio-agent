"""Google Images discovery through a real browser session."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from urllib.parse import quote_plus, urlparse, urlunparse

from .url_safety import URLSafetyError, enforce_remote_url


_GOOGLE_SEARCH = "https://www.google.com/search?q={}&udm=2&hl=pt-BR"
_GOOGLE_HOST_MARKERS = ("google.", "googleusercontent.", "gstatic.com")
_CONSENT_MARKERS = ("consent.google",)
_CAPTCHA_MARKERS = ("captcha", "recaptcha", "/sorry/")
_UNUSUAL_TRAFFIC_MARKERS = ("unusual traffic",)
_IMAGE_EXT = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|bmp)(?:[?#]|$)", re.I)
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_BROWSER_LOCK = Lock()
_TEMP_PATHS: set[str] = set()
_TEMP_PATHS_LOCK = Lock()
_BROWSER_DISABLED = False
_PROJECT_BROWSER_PATH = Path(__file__).resolve().parents[3] / ".playwright-browsers"


def _is_external_http(value: str) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and bool(host) and not any(
        marker in host for marker in _GOOGLE_HOST_MARKERS
    )


def _normalized_url(value: str) -> str:
    try:
        parsed = urlparse(str(value or ""))
        return urlunparse(parsed._replace(fragment=""))
    except ValueError:
        return str(value or "")


def _report(
    report: dict[str, Any] | None,
    *,
    kind: str,
    error: str = "",
    objects: int = 0,
    candidates: int = 0,
    pair_unresolved: int = 0,
) -> None:
    if report is None:
        return
    report.update({
        "http_status": int(report.get("http_status") or 0),
        "html_bytes": int(report.get("html_bytes") or 0),
        "objects_parsed": int(objects),
        "candidates": int(candidates),
        "pair_unresolved": int(pair_unresolved),
        "failure_kind": kind,
        "error": error[:240],
        "parser_version": 2,
    })


def _classify_google_interstitial(*, url: str, title: str, body: str) -> str | None:
    """Classify Google blocks without conflating consent and anti-bot pages."""
    haystack = "\n".join((str(url or ""), str(title or ""), str(body or ""))).casefold()
    if any(marker in haystack for marker in _CAPTCHA_MARKERS):
        return "google_captcha"
    if any(marker in haystack for marker in _UNUSUAL_TRAFFIC_MARKERS):
        return "google_unusual_traffic"
    if any(marker in haystack for marker in _CONSENT_MARKERS):
        return "google_consent_required"
    return None


def _handle_google_consent(page, *, report: dict[str, Any] | None, timeout_ms: int) -> bool:
    """Handle one normal cookie-consent screen; never attempt anti-bot flows."""
    if report is not None:
        report["consent_detected"] = True
    selectors = (
        "button:has-text('Reject all')",
        "button:has-text('Rejeitar tudo')",
        "#W0wltc",
        "[aria-label='Reject all']",
        "[aria-label='Rejeitar tudo']",
    )
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if not button.is_visible(timeout=500):
                continue
            button.click(timeout=min(timeout_ms, 3000), no_wait_after=True)
            page.wait_for_timeout(500)
            if report is not None:
                report["consent_handled"] = True
            return True
        except Exception:  # noqa: BLE001 - selectors vary by locale/DOM
            continue
    if report is not None:
        report["consent_handled"] = False
    return False


def _audit_url(
    url: str,
    *,
    mode: str,
    audit: Callable[[Any], None] | None,
) -> None:
    finding = enforce_remote_url(url, mode=mode)
    if finding is not None and audit is not None:
        try:
            audit(finding)
        except Exception:  # noqa: BLE001 - audit telemetry cannot break discovery
            pass


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
    """Extract one opened result only when image and source share one scope."""
    return page.evaluate(
        """
        () => {
          const bad = /(^|\\.)google\\.|googleusercontent\\.|gstatic\\.com/i;
          const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const imageOk = (img) => {
            const src = img.currentSrc || img.src || '';
            return visible(img) && /^https?:/i.test(src) && !bad.test(src)
              && (img.naturalWidth || img.width || 0) >= 80
              && (img.naturalHeight || img.height || 0) >= 80;
          };
          const sourceOk = (anchor) => {
            const href = anchor.href || '';
            return visible(anchor) && /^https?:/i.test(href) && !bad.test(href)
              && !/\\.(jpe?g|png|webp|gif|avif|bmp)([?#]|$)/i.test(href);
          };
          const roots = [...document.querySelectorAll(
            '[role="dialog"], [aria-modal="true"], [data-result-panel="open"], [data-ri]'
          )].filter(visible);
          const pairs = [];
          for (const root of roots) {
            const images = [...root.querySelectorAll('img')].filter(imageOk);
            const anchors = [...root.querySelectorAll('a[href]')].filter(sourceOk);
            if (images.length !== 1 || anchors.length !== 1) continue;
            const image = images[0];
            const source = anchors[0];
            pairs.push({
              direct_image_url: image.currentSrc || image.src || '',
              thumbnail_url: '',
              source_page_url: source.href || '',
              title: image.alt || (source.innerText || '').trim(),
              width: image.naturalWidth || image.width || 0,
              height: image.naturalHeight || image.height || 0
            });
          }
          if (pairs.length !== 1) return {pair_error: 'google_pair_unresolved'};
          return pairs[0];
        }
        """
    ) or {"pair_error": "google_pair_unresolved"}


def _register_temp_path(path: str) -> None:
    with _TEMP_PATHS_LOCK:
        _TEMP_PATHS.add(path)


def cleanup_browser_artifacts(candidates: list[dict[str, Any]] | None = None) -> int:
    """Remove browser byte artifacts created by this process."""
    paths = {
        str(candidate.get("local_image_path"))
        for candidate in (candidates or [])
        if isinstance(candidate, dict) and candidate.get("local_image_path")
    }
    with _TEMP_PATHS_LOCK:
        paths.update(_TEMP_PATHS)
    removed = 0
    for raw in paths:
        try:
            path = Path(raw)
            if path.name.startswith("unicornio-google-browser-") and path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            continue
        finally:
            with _TEMP_PATHS_LOCK:
                _TEMP_PATHS.discard(raw)
    return removed


def _save_loaded_image(
    context,
    data: dict[str, Any],
    timeout_ms: int,
    *,
    response_cache: dict[str, Any] | None = None,
    remote_url_policy: str = "audit",
    audit: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Persist Chromium response bytes, with a gated request fallback."""
    direct = str(data.get("direct_image_url") or "")
    if not direct:
        return data
    try:
        _audit_url(direct, mode=remote_url_policy, audit=audit)
        response = (response_cache or {}).get(_normalized_url(direct))
        body = response.body() if response is not None else None
        headers = (getattr(response, "headers", {}) or {}) if response is not None else {}
        if not body:
            request = getattr(context, "request", None)
            if request is None:
                return data
            response = request.get(direct, timeout=timeout_ms, fail_on_status_code=False)
            if not getattr(response, "ok", False):
                return data
            body = response.body()
            headers = getattr(response, "headers", {}) or {}
        if not body or len(body) > _MAX_IMAGE_BYTES:
            return data
        mime = str(headers.get("content-type") or "").split(";", 1)[0]
        suffix = ".bin"
        if mime.startswith("image/"):
            suffix = "." + mime.split("/", 1)[1].replace("jpeg", "jpg")
        handle = tempfile.NamedTemporaryFile(
            prefix="unicornio-google-browser-", suffix=suffix, delete=False
        )
        try:
            handle.write(body)
        finally:
            handle.close()
        _register_temp_path(handle.name)
        data["local_image_path"] = handle.name
        data["sha256"] = hashlib.sha256(body).hexdigest()
        data["mime"] = mime
    except URLSafetyError:
        data["pair_error"] = "remote_url_blocked"
    except Exception:  # noqa: BLE001 - browser bytes are an optimization
        return data
    return data


def _set_browser_disabled() -> None:
    global _BROWSER_DISABLED
    _BROWSER_DISABLED = True


def _search_google_browser_images_locked(
    query: str,
    *,
    size: str,
    ratio: str,
    limit: int,
    timeout: float,
    report: dict[str, Any] | None,
    remote_url_policy: str,
    audit: Callable[[Any], None] | None,
) -> list[dict[str, Any]]:
    # Keep the browser alongside the agent instead of depending on a
    # user-specific ~/.cache/ms-playwright path used by cron or Hermes.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_PROJECT_BROWSER_PATH))
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - optional dependency
        _report(report, kind="google_browser_unavailable", error=f"playwright unavailable: {exc}")
        return []

    timeout_ms = max(1000, int(float(timeout) * 1000))
    results: list[dict[str, Any]] = []
    unresolved = 0
    browser = None
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

            def route_guard(route) -> None:
                try:
                    _audit_url(route.request.url, mode=remote_url_policy, audit=audit)
                except URLSafetyError:
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", route_guard)
            page = context.new_page()
            responses: dict[str, Any] = {}

            def capture_response(response) -> None:
                try:
                    resource_type = str(getattr(response.request, "resource_type", "") or "")
                    content_type = str((getattr(response, "headers", {}) or {}).get("content-type") or "")
                    if resource_type == "image" or content_type.casefold().startswith("image/"):
                        responses[_normalized_url(response.url)] = response
                except Exception:  # noqa: BLE001 - capture is best effort
                    pass

            page.on("response", capture_response)
            page.goto(_GOOGLE_SEARCH.format(quote_plus(query)), wait_until="domcontentloaded", timeout=timeout_ms)
            body = (page.content() or "")[:500_000]
            title = page.title()
            if report is not None:
                report["final_url"] = page.url
            block_kind = _classify_google_interstitial(url=page.url, title=title, body=body)
            if block_kind == "google_consent_required":
                _handle_google_consent(page, report=report, timeout_ms=timeout_ms)
                body = (page.content() or "")[:500_000]
                title = page.title()
                if report is not None:
                    report["final_url"] = page.url
                block_kind = _classify_google_interstitial(url=page.url, title=title, body=body)
            if block_kind:
                if report is not None:
                    report["captcha_detected"] = block_kind == "google_captcha"
                    report["unusual_traffic_detected"] = block_kind == "google_unusual_traffic"
                if block_kind in {"google_captcha", "google_unusual_traffic"}:
                    _set_browser_disabled()
                _report(report, kind=block_kind, error=block_kind)
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
                if data.get("pair_error"):
                    unresolved += 1
                    continue
                direct = str(data.get("direct_image_url") or "")
                normalized = _normalized_url(direct)
                if not _is_external_http(direct) or normalized in seen:
                    continue
                seen.add(normalized)
                data = _save_loaded_image(
                    context,
                    data,
                    timeout_ms,
                    response_cache=responses,
                    remote_url_policy=remote_url_policy,
                    audit=audit,
                )
                if data.get("pair_error") == "remote_url_blocked":
                    continue
                results.append(_candidate(query, data))
                if len(results) >= int(limit):
                    break
    except Exception as exc:  # noqa: BLE001 - DOM/browser changes are fail-safe
        _report(
            report,
            kind="google_browser_unavailable",
            error=f"{type(exc).__name__}: {exc}",
            candidates=len(results),
            pair_unresolved=unresolved,
        )
        return results
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass
    kind = "ok" if results else ("google_pair_unresolved" if unresolved else "google_browser_unavailable")
    _report(report, kind=kind, objects=count if "count" in locals() else 0, candidates=len(results), pair_unresolved=unresolved)
    return results


def search_google_browser_images(
    query: str,
    *,
    size: str = "xga",
    ratio: str = "w",
    limit: int = 10,
    timeout: float = 30.0,
    report: dict[str, Any] | None = None,
    remote_url_policy: str = "audit",
    audit: Callable[[Any], None] | None = None,
) -> list[dict[str, Any]]:
    """Discover Google Images through one serialized Playwright session."""
    query = str(query or "").strip()
    if not query:
        return []
    if os.environ.get("EDITOR_GOOGLE_BROWSER_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        _report(report, kind="google_browser_unavailable", error="provider disabled")
        return []
    with _BROWSER_LOCK:
        if _BROWSER_DISABLED:
            _report(report, kind="google_browser_unavailable", error="browser disabled after prior block")
            return []
        return _search_google_browser_images_locked(
            query,
            size=size,
            ratio=ratio,
            limit=limit,
            timeout=timeout,
            report=report,
            remote_url_policy=remote_url_policy,
            audit=audit,
        )


__all__ = ["search_google_browser_images", "cleanup_browser_artifacts", "_extract_open_result"]
