"""Verificacao de que uma imagem baixada esta de fato listada na pagina de origem.

A gate textual de relevancia valida alt/credit/URL — o slug de uma galeria
pode dizer "green-lantern" enquanto o CDN serve bytes de outra obra (galerias
dinamicas, hotlink fallback, conteudo rotativo). Esta verificacao roda APOS o
download e compara a imagem baixada com as imagens realmente listadas na
pagina de origem (``<img>``, ``og:image``, ``srcset``): fail-closed — se a
pagina nao lista a imagem, ou os bytes divergem da variante listada, o item e
rejeitado ("nenhuma imagem > imagem errada").
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
from .url_safety import inspect_remote_url

_PAGE_MAX_BYTES = 2 * 1024 * 1024
_IMG_MAX_BYTES = 8 * 1024 * 1024
_VERIFY_TOTAL_MAX_BYTES = 32 * 1024 * 1024
_MAX_DOWNLOADS = 6
_FETCH_TIMEOUT = 15
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_IMG_EXT = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|bmp)(?:\?|#|$)", re.IGNORECASE)
_SRC_RE = re.compile(r'<img\b[^>]*\b(?:src|data-src)=["\']([^"\']+)', re.IGNORECASE)
_SRCSET_RE = re.compile(r'<(?:img|source)\b[^>]*\b(?:srcset|data-srcset)=["\']([^"\']+)', re.IGNORECASE)
_OG_IMAGE_RE = re.compile(
    r'<meta\b[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _fetch(url: str, accept: str, max_bytes: int, budget: list[int], audit=None) -> bytes | None:
    if budget[0] <= 0:
        return None
    try:
        finding = inspect_remote_url(url)
        if finding and audit:
            audit(finding)
        request = Request(
            url,
            headers={"User-Agent": _UA, "Accept": accept},
        )
        with urlopen(request, timeout=_FETCH_TIMEOUT) as response:
            finding = inspect_remote_url(response.geturl())
            if finding and audit:
                audit(finding)
            data = response.read(min(max_bytes, budget[0]) + 1)
            if len(data) > max_bytes:
                return None
            budget[0] -= len(data)
            return data
    except (HTTPError, URLError, OSError, ValueError):
        return None


def _slug(url: str) -> str:
    """Nome base do arquivo (sem extensao/query) para casar variantes."""
    path = urlparse(url).path
    base = unquote(path.rsplit("/", 1)[-1])
    return re.sub(r"\.(?:jpe?g|png|webp|gif|avif|bmp)$", "", base, flags=re.I).lower()


def _image_urls_in_page(html: str, base_url: str) -> list[str]:
    """URLs de imagem listadas na pagina (img/src, srcset, og:image)."""
    urls: list[str] = []
    for match in _SRC_RE.finditer(html):
        urls.append(match.group(1))
    for match in _SRCSET_RE.finditer(html):
        for candidate in match.group(1).split(","):
            token = candidate.strip().split(" ")[0]
            if token:
                urls.append(token)
    for match in _OG_IMAGE_RE.finditer(html):
        urls.append(match.group(1))
    resolved: list[str] = []
    seen: set[str] = set()
    for url in urls:
        full = urljoin(base_url, url.strip())
        if not _IMG_EXT.search(full):
            continue
        if full in seen:
            continue
        seen.add(full)
        resolved.append(full)
    # Preserve the complete set found inside the bounded 2 MiB page. The old
    # first-12 truncation rejected a valid direct_image_url merely because a
    # theme placed it later in the markup. Download limits are enforced when
    # comparing candidates, not while discovering the exact URL.
    return resolved


def _normalized_url(url: str) -> str:
    """Normalize harmless URL differences for exact source-page matching."""
    parsed = urlparse(unquote(url.strip()))
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", parsed.query, "")
    )


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def verify_downloaded_against_source(
    *,
    source_page_url: str,
    downloaded: Path,
    direct_image_url: str,
    cache: dict[str, list[str] | None] | None = None,
    audit=None,
) -> tuple[bool, str]:
    """Confirma que ``downloaded`` corresponde a uma imagem listada na pagina.

    Returns ``(ok, reason)``. Fail-closed: pagina inacessivel, imagem nao
    listada, ou bytes divergentes -> ``(False, motivo)``.
    """
    if not isinstance(source_page_url, str) or not source_page_url.strip():
        return False, "source_page_url ausente; impossivel verificar a imagem contra a origem"
    cache = cache if cache is not None else {}
    budget = [_VERIFY_TOTAL_MAX_BYTES]
    if source_page_url not in cache:
        page_html = _fetch(source_page_url, "text/html", _PAGE_MAX_BYTES, budget, audit)
        cache[source_page_url] = (
            _image_urls_in_page(page_html.decode("utf-8", "ignore"), source_page_url)
            if page_html is not None
            else None
        )
    listed = cache[source_page_url]
    if not listed:
        return False, (
            "pagina de origem inacessivel ou sem imagens listadas; "
            "nao e possivel confirmar a origem da imagem (fonte instavel)"
        )
    try:
        downloaded_hash = _md5(downloaded.read_bytes())
    except OSError as exc:
        return False, f"falha ao ler o arquivo baixado: {exc}"

    direct_normalized = _normalized_url(direct_image_url)
    exact = [url for url in listed if _normalized_url(url) == direct_normalized]
    for url in exact:
        data = _fetch(url, "image/*", _IMG_MAX_BYTES, budget, audit)
        if data is not None and _md5(data) == downloaded_hash:
            return True, "URL exata confirmada na pagina de origem (bytes iguais)"
    if exact:
        return (
            False,
            "CDN serviu conteudo divergente da URL exata listada na pagina de origem "
            "(bytes diferentes); fonte instavel — troque a URL da imagem",
        )

    slug = _slug(direct_image_url)
    same_slug = [url for url in listed if _slug(url) == slug]
    downloads = 0
    for url in same_slug:
        if downloads >= _MAX_DOWNLOADS:
            break
        downloads += 1
        data = _fetch(url, "image/*", _IMG_MAX_BYTES, budget, audit)
        if data is not None and _md5(data) == downloaded_hash:
            return True, "imagem confirmada na pagina de origem por slug e bytes"
    if same_slug:
        return (
            False,
            "CDN serviu conteudo divergente da pagina de origem para a mesma imagem "
            "(bytes diferentes); fonte instavel — troque a URL da imagem",
        )
    for url in listed:
        if downloads >= _MAX_DOWNLOADS:
            break
        downloads += 1
        data = _fetch(url, "image/*", _IMG_MAX_BYTES, budget, audit)
        if data is not None and _md5(data) == downloaded_hash:
            return True, "imagem confirmada na pagina de origem (bytes iguais)"
    return (
        False,
        "imagem baixada nao consta na pagina de origem (slug ausente e bytes nao "
        "correspondem a nenhuma imagem listada); troque por uma imagem da pagina",
    )
