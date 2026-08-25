"""Discovery of image candidates via Google Images with size/ratio filters.

Deterministic and read-only: builds the same advanced-search URL an editor
uses by hand (`imgsz` = size class, `imgar` = aspect ratio, `udm=2` =
image results) and returns candidates so the LLM does not have to reason about
search URLs or parse results — that cost is moved to code (token economy).

IMPORTANT policy: Google Images is only a DISCOVERY INDEX, never the source.
The `direct_image_url` returned is the real image URL found in the result
metadata (the page's own image, not the Google preview thumbnail). The agent
must still open `source_page_url` and confirm the image is listed there
(`verify_downloaded_against_source` in the apply) and register credit.

Size classes (Google `imgsz`):
  ic  = icon / small       | xga  = 1024x768   | vga  = 640x480
  qsvga = 400x300          | m    = medium     | n    = large
  l   = extra large        | xxl  = 2mp+       | qhd  = 1920x1080
Aspect ratios (Google `imgar`):
  t  = tall (t)  | s  = square (s)  | w  = wide / landscape (w)
  x  = panoramic (x)

The default mirrors the editor's manual flow: `imgsz=xga` (1024x768) and
`imgar=w` (wide/landscape), the same size the portal publishes at.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

_GOOGLE_IMAGES_BASE = "https://www.google.com/search"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_MAX_BYTES = 3 * 1024 * 1024
_MAX_RESULTS = 20

# Allowed Google size / aspect tokens (fail-closed on unknown values).
_SIZES = {"ic", "xga", "vga", "qsvga", "m", "n", "l", "xxl", "qhd"}
_RATIOS = {"t", "s", "w", "x"}
_SIZE_LABEL = {"xga": "1024x768"}

# Regexes over the results page to extract candidate metadata.
# Google embeds per-result data (thumbnail, original image URL, page URL,
# title) inside script JSON. We parse defensively and drop anything that does
# not yield a real (non-googleusercontent preview) image URL.
_THUMB_KEY_RE = re.compile(r'"tu":\s*"([^"]+)"')
_SRC_KEY_RE = re.compile(r'"ou":\s*"([^"]+)"')
_PAGE_KEY_RE = re.compile(r'"ru":\s*"([^"]+)"')
_TITLE_KEY_RE = re.compile(r'"pt":\s*"([^"]+)"')


def build_search_url(query: str, *, size: str = "xga", ratio: str = "w") -> str:
    """Google Images advanced-search URL with size/ratio filters applied."""
    size = (size or "xga").strip()
    ratio = (ratio or "w").strip()
    if size not in _SIZES:
        size = "xga"
    if ratio not in _RATIOS:
        ratio = "w"
    params = {
        "as_st": "y",
        "as_q": query,
        "as_epq": "",
        "as_eq": "",
        "as_sitesearch": "",
        "imgsz": size,
        "imgar": ratio,
        "cr": "",
        "as_filetype": "",
        "tbs": "",
        "authuser": "0",
        "udm": "2",
    }
    return f"{_GOOGLE_IMAGES_BASE}?{urlencode(params)}"


class MediaSearchError(RuntimeError):
    """Raised when an image search cannot be performed."""


def search_web_images(
    query: str,
    *,
    size: str = "xga",
    ratio: str = "w",
    limit: int = 10,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` image candidates from a filtered Google Images search.

    Each candidate carries the discovery `query` and size filter so the gate
    can use it as relevance evidence, plus `direct_image_url` / `source_page_url`
    / `title` / `thumbnail_url`. Fail-closed: unusable entries are dropped;
    an unreachable/unparseable search returns an empty list (never raises).
    """
    query = (query or "").strip()
    if not query:
        return []
    size = size if size in _SIZES else "xga"
    ratio = ratio if ratio in _RATIOS else "w"
    url = build_search_url(query, size=size, ratio=ratio)
    try:
        request = Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
        with urlopen(request, timeout=timeout) as response:
            html = response.read(_MAX_BYTES + 1)
    except (HTTPError, URLError, OSError, ValueError):
        return []
    if len(html) > _MAX_BYTES:
        html = html[:_MAX_BYTES]
    page = html.decode("utf-8", "ignore")

    thumbs = _THUMB_KEY_RE.findall(page)
    sources = _SRC_KEY_RE.findall(page)
    pages = _PAGE_KEY_RE.findall(page)
    titles = _TITLE_KEY_RE.findall(page)

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, thumb in enumerate(thumbs):
        direct = sources[i] if i < len(sources) else ""
        source_page = pages[i] if i < len(pages) else ""
        title = titles[i] if i < len(titles) else ""
        if not direct:
            continue
        if not _real_image_url(direct):
            continue
        if direct in seen:
            continue
        seen.add(direct)
        results.append(
            {
                "query": query,
                "size_filter": f"{_SIZE_LABEL.get(size, size)}|{ratio}",
                "title": title[:200],
                "direct_image_url": direct,
                "source_page_url": _clean_page_url(source_page),
                "thumbnail_url": _unescape(thumb),
            }
        )
        if len(results) >= limit:
            break
    return results


def _real_image_url(url: str) -> bool:
    """True when the URL points to an actual image host (not a Google preview)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host or not parsed.scheme:
        return False
    # Google preview thumbnails (encrypted-tbn...) are NOT the source.
    if "googleusercontent" in host or "gstatic.com" in host:
        return False
    return True


def _clean_page_url(url: str) -> str:
    return _unescape(url).split("&")[0]


def _unescape(value: str) -> str:
    """Decode backslash-u-XXXX escapes and basic HTML escapes Google embeds."""
    if not value:
        return ""
    try:
        value = json.loads(f'"{value}"')
    except (ValueError, json.JSONDecodeError):
        pass
    return value


__all__ = ["build_search_url", "search_web_images", "MediaSearchError"]
