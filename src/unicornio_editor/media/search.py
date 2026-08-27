"""Discovery of image candidates via multiple search engines (Bing, Google, Yandex).

Deterministic and read-only: builds the search URL for each engine and returns
image candidates so the LLM does not have to reason about search URLs or parse
results - that cost is moved to code (token economy).

Google Images blocks datacenter/cloud IPs (CAPTCHA/Cloudflare), so in production
it frequently returns an empty/unparseable page. To stay robust, the pipeline
tries engines in order and rotates on failure:

  1. Bing Images  (primary - reliable from datacenter IPs)
  2. Google Images (fallback - index only, never the source)
  3. Yandex Images (fallback)

IMPORTANT policy: the search engine is only a DISCOVERY INDEX, never the source.
The direct_image_url returned is the real image URL found in the result
metadata (the page's own image, not the engine's preview thumbnail). The agent
must still open source_page_url and confirm the image is listed there
(verify_downloaded_against_source in the apply) and register credit.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

_GOOGLE_IMAGES_BASE = "https://www.google.com/search"
_BING_IMAGES_BASE = "https://www.bing.com/images/search"
_YANDEX_IMAGES_BASE = "https://yandex.com/images/search"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_MAX_BYTES = 3 * 1024 * 1024

# Allowed Google size / aspect tokens (fail-closed on unknown values).
_SIZES = {"ic", "xga", "vga", "qsvga", "m", "n", "l", "xxl", "qhd"}
_RATIOS = {"t", "s", "w", "x"}
_SIZE_LABEL = {"xga": "1024x768"}
# Map generic size to Bing custom filter (1024x768 -> custom_1024_768).
_BING_SIZE_FILTER = {"xga": "filterui:imagesize-custom_1024_768"}

# Google result keys (tu/ou/ru/pt).
_THUMB_KEY_RE = re.compile(r'"tu":\s*"([^"]+)"')
_SRC_KEY_RE = re.compile(r'"ou":\s*"([^"]+)"')
_PAGE_KEY_RE = re.compile(r'"ru":\s*"([^"]+)"')
_TITLE_KEY_RE = re.compile(r'"pt":\s*"([^"]+)"')

# Bing embeds JSON with HTML-escaped quotes: &quot;purl&quot;:&quot;PAGE&quot;
_BING_PURL_RE = re.compile(r'"purl":"([^"]+)"')
_BING_MURL_RE = re.compile(r'"murl":"([^"]+)"')
_BING_TURL_RE = re.compile(r'"turl":"([^"]+)"')


class MediaSearchError(RuntimeError):
    """Raised when an image search cannot be performed."""


def _fetch(url: str, timeout: float) -> str:
    request = Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
    with urlopen(request, timeout=timeout) as response:
        data = response.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        data = data[:_MAX_BYTES]
    return data.decode("utf-8", "ignore")


def _real_image_url(url: str) -> bool:
    """True when the URL points to an actual image host (not a search preview)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host or not parsed.scheme:
        return False
    # Engine preview thumbnails are NOT the source.
    if "googleusercontent" in host or "gstatic.com" in host:
        return False
    if "bing.net" in host or "yastatic" in host or "mds.yandex" in host:
        return False
    return True


def _clean_page_url(url: str) -> str:
    return _unescape(url).split("&")[0]


def _unescape(value: str) -> str:
    """Decode backslash-u-XXXX escapes and basic HTML escapes."""
    if not value:
        return ""
    try:
        value = json.loads(f'"{value}"')
    except (ValueError, json.JSONDecodeError):
        pass
    return value


def _candidate(query, size_filter, direct, page, title, thumb):
    return {
        "query": query,
        "size_filter": size_filter,
        "title": (title or "")[:200],
        "direct_image_url": direct,
        "source_page_url": _clean_page_url(page),
        "thumbnail_url": thumb,
    }


# ---------------------------------------------------------------------------
# Bing Images
# ---------------------------------------------------------------------------

def build_bing_url(query: str, *, size: str = "xga") -> str:
    qft = ""
    if size in _BING_SIZE_FILTER:
        qft = "+" + _BING_SIZE_FILTER[size]
    return f"{_BING_IMAGES_BASE}?q={quote_plus(query)}&qft={qft}&form=IRFLTR&first=1"


def search_bing_images(
    query: str,
    *,
    size: str = "xga",
    ratio: str = "w",
    limit: int = 10,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Bing Images candidates (primary). Returns purl(page)+murl(img)."""
    query = (query or "").strip()
    if not query:
        return []
    size = size if size in _SIZES else "xga"
    url = build_bing_url(query, size=size)
    try:
        page = _fetch(url, timeout)
    except (HTTPError, URLError, OSError, ValueError):
        return []
    html = page.replace("&quot;", '"')
    purls = [m.group(1) for m in _BING_PURL_RE.finditer(html)]
    murls = [m.group(1) for m in _BING_MURL_RE.finditer(html)]
    turls = [m.group(1) for m in _BING_TURL_RE.finditer(html)]
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, direct in enumerate(murls):
        if not direct or not _real_image_url(direct):
            continue
        if direct in seen:
            continue
        seen.add(direct)
        page_url = purls[i] if i < len(purls) else ""
        thumb = turls[i] if i < len(turls) else ""
        results.append(_candidate(query, "1024x768|w", direct, page_url, "", thumb))
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# Google Images
# ---------------------------------------------------------------------------

def build_search_url(query: str, *, size: str = "xga", ratio: str = "w") -> str:
    size = (size or "xga").strip()
    ratio = (ratio or "w").strip()
    if size not in _SIZES:
        size = "xga"
    if ratio not in _RATIOS:
        ratio = "w"
    params = {
        "as_st": "y", "as_q": query, "as_epq": "", "as_eq": "",
        "as_sitesearch": "", "imgsz": size, "imgar": ratio, "cr": "",
        "as_filetype": "", "tbs": "", "authuser": "0", "udm": "2",
    }
    return f"{_GOOGLE_IMAGES_BASE}?{urlencode(params)}"


def search_google_images(
    query: str,
    *,
    size: str = "xga",
    ratio: str = "w",
    limit: int = 10,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    size = size if size in _SIZES else "xga"
    ratio = ratio if ratio in _RATIOS else "w"
    url = build_search_url(query, size=size, ratio=ratio)
    try:
        page = _fetch(url, timeout)
    except (HTTPError, URLError, OSError, ValueError):
        return []
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
        if not direct or not _real_image_url(direct) or direct in seen:
            continue
        seen.add(direct)
        results.append(_candidate(query, f"{_SIZE_LABEL.get(size, size)}|{ratio}", direct, source_page, title, _unescape(thumb)))
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# Yandex Images
# ---------------------------------------------------------------------------

def build_yandex_url(query: str) -> str:
    return f"{_YANDEX_IMAGES_BASE}?{urlencode({'text': query})}"


def search_yandex_images(
    query: str,
    *,
    size: str = "xga",
    ratio: str = "w",
    limit: int = 10,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    url = build_yandex_url(query)
    try:
        page = _fetch(url, timeout)
    except (HTTPError, URLError, OSError, ValueError):
        return []
    html = page.replace("&quot;", '"')
    # O Yandex embute a URL real da imagem no parametro img_url= (URL-encoded)
    # dos itens de resultado — exatamente o que o botao "Open" da UI usa. Um
    # unquote revela a URL direta (ex.: i.pinimg.com/...jpg).
    img_urls: list[str] = []
    for raw in re.findall(r'img_url=([^&]+)', page):
        decoded = unquote(raw)
        if decoded.startswith("http"):
            img_urls.append(decoded)
    if not img_urls:
        img_urls = re.findall(r'<img[^>]+src="(https?://[^"]+)"', page)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for direct in img_urls:
        if not direct or not _real_image_url(direct) or direct in seen:
            continue
        seen.add(direct)
        results.append(_candidate(query, "1024x768|w", direct, "", "", ""))
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# Rotating facade
# ---------------------------------------------------------------------------

def search_web_images(
    query: str,
    *,
    size: str = "xga",
    ratio: str = "w",
    limit: int = 10,
    timeout: float = 30.0,
    engine: str = "auto",
) -> list[dict[str, Any]]:
    """Rotate through search engines until one returns candidates.

    Order: Bing (primary) -> Google (fallback) -> Yandex (fallback). When
    engine is a concrete name, only that engine is used. Fail-closed: always
    returns a list (possibly empty), never raises.
    """
    query = (query or "").strip()
    if not query:
        return []
    engines: list[tuple[str, Any]] = []
    if engine in ("auto", "bing"):
        engines.append(("bing", search_bing_images))
    if engine in ("auto", "google"):
        engines.append(("google", search_google_images))
    if engine in ("auto", "yandex"):
        engines.append(("yandex", search_yandex_images))
    for name, fn in engines:
        try:
            last = fn(query, size=size, ratio=ratio, limit=limit, timeout=timeout)
        except Exception:  # noqa: BLE001 - rotate on any failure
            last = []
        if last:
            for c in last:
                c["engine"] = name
            return last
    return []


__all__ = [
    "build_search_url", "build_bing_url", "build_yandex_url",
    "search_web_images", "search_bing_images", "search_google_images",
    "search_yandex_images", "MediaSearchError",
]
