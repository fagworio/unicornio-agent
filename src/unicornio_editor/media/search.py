"""Discovery of image candidates via multiple search engines (Bing, Google, Yandex).

Deterministic and read-only: builds the search URL for each engine and returns
image candidates so the LLM does not have to reason about search URLs or parse
results - that cost is moved to code (token economy).

Google Images não entrega mais resultados no HTML servido (2026-09-24): qualquer
endpoint (`udm=2`, `tbm=isch`, `images.google.com`, UA mobile) devolve ~92 KB de
bootstrap com `<noscript>` + redirect para `/httpservice/retry/enablejs` — zero
`<img>`, zero chaves de resultado. Ou seja: NÃO é rate-limit nem captcha, é a
página "ative o JavaScript". O parser (chaves `tu`/`ou`/`ru`/`pt`) continua
correto para páginas de resultado; o que mudou é o que o servidor manda. Por isso
a falha é CLASSIFICADA (``failure_kind``) antes de alimentar o circuit breaker:
``parser_schema_drift``/``js_required``/``captcha`` NÃO são indisponibilidade
transitória e não podem renovar cooldown (era o que deixava o Google fora do ar
para sempre, contado como 18 "falhas" seguidas). O Google permanece na ordem
(último), é sempre tentado e sempre deixa telemetria do motivo.

``search_web_images`` tenta primeiro o Google via navegador real. Se o browser
estiver indisponível, usa Bing e depois Yandex em ordem fixa:

  1. Google Browser (discovery + página exibida, nunca a fonte)
  2. Bing Images or Yandex Images (fallback, alternates by query)
  3. the other of the two

IMPORTANT policy: the search engine is only a DISCOVERY INDEX, never the source.
The direct_image_url returned is the real image URL found in the result
metadata (the page's own image, not the engine's preview thumbnail). The agent
must still open source_page_url and confirm the image is listed there
(verify_downloaded_against_source in the apply) and register credit.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import ipaddress
import json
import os
import random
import sys
import time
from threading import Lock
from pathlib import Path
import re
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse, urlsplit
from urllib.request import Request, urlopen

from .page_assets import is_noise_image_url

_GOOGLE_IMAGES_BASE = "https://www.google.com/search"
_BING_IMAGES_BASE = "https://www.bing.com/images/search"
_YANDEX_IMAGES_BASE = "https://yandex.com/images/search"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_MAX_BYTES = 3 * 1024 * 1024
_MAX_BATCH_QUERIES = 20
_BATCH_WORKERS = 4
_HTTP_REQUESTS = 0
_HTTP_REQUESTS_LOCK = Lock()


def reset_http_request_count() -> None:
    """Reset the process-local counter used by batch economics telemetry."""
    global _HTTP_REQUESTS
    with _HTTP_REQUESTS_LOCK:
        _HTTP_REQUESTS = 0


def http_request_count() -> int:
    with _HTTP_REQUESTS_LOCK:
        return int(_HTTP_REQUESTS)

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



# ---------------------------------------------------------------------------
# Saúde por engine: classificação da falha + relatório por tentativa
# ---------------------------------------------------------------------------
PARSER_VERSION = 2

# Motivos possíveis de uma tentativa de engine. A distinção existe porque só
# indisponibilidade TRANSITÓRIA justifica cooldown: misturar "o HTML mudou" com
# "estou com rate-limit" tirou o Google da arquitetura para sempre (18 "falhas"
# seguidas renovando cooldown de 12 min sem que nada estivesse fora do ar).
FAILURE_KINDS = (
    "ok",
    "cooldown_skip",          # engine pulada por cooldown transitório ativo
    "network_error",          # DNS/timeout/conexão — transitório
    "rate_limited",           # 429/503 — transitório
    "http_error",             # 4xx/5xx inesperado — transitório
    "captcha",                # interstitial de captcha/consent — NÃO transitório
    "js_required",            # página exige JavaScript: sem resultados no HTML
    "parser_schema_drift",    # página de resultados com schema novo (nossas chaves sumiram)
    "no_results_legitimate",  # página de resultados, realmente sem resultado para a query
)
FAILURE_KINDS_TRANSITORIOS = frozenset({"network_error", "rate_limited", "http_error"})

_MARKERS_CAPTCHA = ("unusual traffic", "/sorry/", "sorry/index", "recaptcha", "captcha")
_MARKERS_JS = ("/httpservice/retry/enablejs", "enablejs")
# Marcadores de que a página É de resultados (thumbnails/blobs de imagem no HTML):
# é o que separa "o schema mudou" de "não havia resultado para esta query".
_MARKERS_RESULTADOS = (
    "googleusercontent", "encrypted-tbn", "gstatic.com/images",
    "<img", "af_initdatacallback",
)

# Último relatório por engine (conveniência para o caminho de busca única; o
# caminho em lote recebe o relatório explícito por query).
_ULTIMO_RELATORIO: dict[str, dict[str, Any]] = {}

# O nome da engine no relatório não precisa ser a chave operacional do
# circuit breaker. Bing Images e Bing Web Search são serviços independentes.
_PROVIDER_KEYS = {
    "bing": "bing_images",
    "yandex": "yandex_images",
    "google": "google_images",
    "google_browser": "google_browser",
}


def provider_key(engine: str) -> str:
    """Retorna a chave operacional do breaker para uma engine de busca."""
    name = str(engine or "").strip()
    return _PROVIDER_KEYS.get(name, name)


def classify_failure(
    html: str,
    *,
    http_status: int = 200,
    objects_parsed: int = 0,
    error: str = "",
) -> str:
    """Classifica UMA tentativa de engine, na ordem erro -> bloqueio -> schema.

    ``HTTP 200 + HTML grande + 0 objetos`` NÃO é rate-limit: ou o servidor mandou
    um interstitial (captcha / "ative o JavaScript"), ou o schema dos resultados
    mudou. Só a primeira família (transitória) pode alimentar o cooldown.
    """
    if error:
        return "network_error"
    if http_status in (429, 503):
        return "rate_limited"
    if http_status >= 400 or http_status == 0:
        return "http_error"
    if objects_parsed > 0:
        return "ok"
    corpo = (html or "").lower()
    if any(marcador in corpo for marcador in _MARKERS_CAPTCHA):
        return "captcha"
    if any(marcador in corpo for marcador in _MARKERS_JS):
        return "js_required"
    if any(marcador in corpo for marcador in _MARKERS_RESULTADOS):
        return "parser_schema_drift"
    return "no_results_legitimate"


def _finalizar_relatorio(
    relatorio: dict[str, Any],
    saida: dict[str, Any] | None,
    *,
    engine: str,
    objects: int,
    candidates: int,
    html: str = "",
) -> dict[str, Any]:
    """Completa o relatório da tentativa, publica em ``saida``/registry e o devolve."""
    relatorio["objects_parsed"] = int(objects)
    relatorio["candidates"] = int(candidates)
    if not relatorio.get("error") and relatorio.get("failure_kind") not in ("rate_limited", "http_error"):
        relatorio["failure_kind"] = classify_failure(
            html,
            http_status=int(relatorio.get("http_status") or 0),
            objects_parsed=int(objects),
        )
    relatorio["parser_version"] = PARSER_VERSION
    _ULTIMO_RELATORIO[engine] = dict(relatorio)
    if saida is not None:
        saida.clear()
        saida.update(relatorio)
    return relatorio


def engine_last_report(engine: str) -> dict[str, Any]:
    """Relatório da última tentativa da engine (vazio quando nunca foi tentada)."""
    return dict(_ULTIMO_RELATORIO.get(engine) or {})


def _fetch_report(url: str, timeout: float) -> tuple[str | None, dict[str, Any]]:
    """Baixa a página e devolve ``(html, relatório)`` — nunca levanta.

    O relatório carrega status/bytes/motivo da falha para que a camada de
    telemetria registre POR QUE uma engine não entregou nada (antes isso era
    silencioso: a engine simplesmente não aparecia no JSON).
    """
    global _HTTP_REQUESTS
    relatorio: dict[str, Any] = {
        "http_status": 0, "html_bytes": 0, "error": "",
        "failure_kind": "network_error", "parser_version": PARSER_VERSION,
    }
    with _HTTP_REQUESTS_LOCK:
        _HTTP_REQUESTS += 1
    request = Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            data = response.read(_MAX_BYTES + 1)
    except HTTPError as exc:
        codigo = int(getattr(exc, "code", 0) or 0)
        relatorio["http_status"] = codigo
        relatorio["error"] = f"HTTPError {codigo}"
        relatorio["failure_kind"] = "rate_limited" if codigo in (429, 503) else "http_error"
        return None, relatorio
    except (URLError, OSError, ValueError) as exc:
        relatorio["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        relatorio["failure_kind"] = "network_error"
        return None, relatorio
    if len(data) > _MAX_BYTES:
        data = data[:_MAX_BYTES]
    html = data.decode("utf-8", "ignore")
    relatorio["http_status"] = status
    relatorio["html_bytes"] = len(data)
    return html, relatorio


def _fetch(url: str, timeout: float) -> str:
    """Compatibilidade: HTML da página ou exceção (``_fetch_report`` faz o resto)."""
    html, relatorio = _fetch_report(url, timeout)
    if html is None:
        raise URLError(str(relatorio.get("error") or "fetch failed"))
    return html


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
    # Preserva a URL literal e seus parâmetros; alguns CDNs usam a variante
    # exata (inclusive query string) para selecionar os bytes da página.
    return _unescape(url).strip()


def _unescape(value: str) -> str:
    """Decode backslash-u-XXXX escapes and basic HTML escapes."""
    if not value:
        return ""
    try:
        value = json.loads(f'"{value}"')
    except (ValueError, json.JSONDecodeError):
        pass
    return value


def _valid_http(url: str) -> bool:
    """URL http(s) absoluta com host (Fase 4)."""
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _candidate(
    query,
    size_filter,
    direct,
    page,
    title,
    thumb,
    *,
    engine="",
    discovery_method="",
):
    """Candidato normalizado — com `usable` explicito (Fase 4).

    A cadeia verificavel e query -> pagina de origem -> URL da imagem -> bytes.
    Sem `source_page_url` nao existe cadeia: o candidato do Yandex (que entrega
    a imagem sem a pagina) fica `usable=False` + `discovery_only`, e nunca deve
    entrar no media_plan. Antes ele entrava com source vazio e so era
    descoberto no apply (depois de baixar e gastar upload).
    """
    page_clean = _clean_page_url(page)
    noisy = is_noise_image_url(direct)
    usable = _valid_http(direct) and _valid_http(page_clean) and not noisy
    candidate_id = hashlib.sha256(
        f"{engine}|{query}|{direct}".encode("utf-8", "ignore")
    ).hexdigest()[:20]
    motivo = ""
    if not usable:
        if noisy:
            motivo = "noise_domain"
        elif not _valid_http(page_clean):
            motivo = "missing_source_page"
        else:
            motivo = "invalid_direct_image_url"
    return {
        "candidate_id": candidate_id,
        "query": query,
        "discovery_image_url": direct,
        "size_filter": size_filter,
        "title": (title or "")[:200],
        "direct_image_url": direct,
        "source_page_url": page_clean,
        "thumbnail_url": thumb,
        "engine": engine,
        "discovery_method": discovery_method,
        "usable": usable,
        "discovery_only": not usable,
        "rejected_reason": motivo,
    }


# ---------------------------------------------------------------------------
# Bing Images
# ---------------------------------------------------------------------------

_BING_M_ATTR_RE = re.compile(r'\bm="(\{.*?\})"', re.DOTALL)


def _bing_result_objects(html: str) -> list[dict[str, Any]]:
    """Objetos de resultado do Bing (Fase 14: um candidato por objeto).

    1. Formato principal: o JSON no atributo ``m`` de cada resultado — as
       chaves purl/murl/turl/t vivem JUNTAS no mesmo objeto.
    2. Fallback (páginas que só trazem as chaves soltas): agrupa por
       PROXIMIDADE — cada bloco começa em um ``murl`` e lê as chaves daquele
       trecho. Nunca associa por índice entre listas separadas (era assim que a
       imagem acabava ligada à página de OUTRO resultado quando um deles não
       tinha purl).
    """
    objects: list[dict[str, Any]] = []
    # O HTML do Bing chega com as aspas do JSON escapadas como &quot;. Decodifica
    # aqui dentro (o chamador pode passar o html cru) — sem isso o atributo
    # `m="{...}"` fica invisivel e o parser cai no fallback (que nao tem a
    # pagina de origem de cada resultado).
    html = html.replace("&quot;", '"')
    for raw in _BING_M_ATTR_RE.findall(html):
        try:
            data = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("murl"):
            objects.append(data)
    if objects:
        return objects
    for bloco in re.split(r'(?="murl"\s*:)', html)[1:]:
        janela = bloco[:1200]
        murl = re.search(r'"murl"\s*:\s*"([^"]+)"', janela)
        if not murl:
            continue
        purl = re.search(r'"purl"\s*:\s*"([^"]+)"', janela)
        titulo = re.search(r'"t"\s*:\s*"([^"]+)"', janela)
        turl = re.search(r'"turl"\s*:\s*"([^"]+)"', janela)
        objects.append({
            "murl": _unescape(murl.group(1)),
            "purl": _unescape(purl.group(1)) if purl else "",
            "t": _unescape(titulo.group(1)) if titulo else "",
            "turl": _unescape(turl.group(1)) if turl else "",
        })
    return objects


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
    report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Bing Images candidates (primary). Returns purl(page)+murl(img)."""
    query = (query or "").strip()
    if not query:
        return []
    size = size if size in _SIZES else "xga"
    url = build_bing_url(query, size=size)
    page, relatorio = _fetch_report(url, timeout)
    if page is None:
        _finalizar_relatorio(relatorio, report, engine="bing", objects=0, candidates=0)
        return []
    html = page.replace("&quot;", '"')
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    objetos = _bing_result_objects(html)
    # Fase 14: cada resultado do Bing carrega o proprio JSON no atributo `m`
    # (murl+purl+turl do MESMO resultado). O parser antigo coletava as tres
    # listas separadamente e associava por INDICE: quando um resultado nao
    # tinha purl (ou a ordem divergia), a imagem era ligada a pagina de OUTRO
    # resultado — origem errada, verificacao de origem inutil.
    for obj in objetos:
        direct = str(obj.get("murl") or "")
        if not direct or not _real_image_url(direct) or direct in seen:
            continue
        seen.add(direct)
        results.append(
            _candidate(
                query, "1024x768|w", direct,
                str(obj.get("purl") or ""),
                str(obj.get("t") or ""),
                str(obj.get("turl") or ""),
                engine="bing",
                discovery_method="bing_result",
            )
        )
        if len(results) >= limit:
            break
    _finalizar_relatorio(
        relatorio, report, engine="bing",
        objects=len(objetos), candidates=len(results), html=html,
    )
    return results


# ---------------------------------------------------------------------------
# Google Images
# ---------------------------------------------------------------------------

def _google_result_objects(html: str) -> list[dict[str, Any]]:
    """Resultados do Google (Fase 14): um objeto por segmento de resultado.

    O HTML do Google embute as chaves ``tu``/``ou``/``ru``/``pt`` dentro do
    mesmo trecho de cada resultado. Dividimos pelo marcador ``"ou"`` (a imagem
    real) e lemos as chaves DENTRO da janela daquele resultado.
    """
    objects: list[dict[str, Any]] = []
    for parte in re.split(r'(?="ou"\s*:)', html)[1:]:
        janela = parte[:4000]
        ou = _SRC_KEY_RE.search(janela)
        if not ou:
            continue
        ru = _PAGE_KEY_RE.search(janela)
        pt = _TITLE_KEY_RE.search(janela)
        tu = _THUMB_KEY_RE.search(janela)
        objects.append({
            "murl": _unescape(ou.group(1)),
            "purl": _unescape(ru.group(1)) if ru else "",
            "t": _unescape(pt.group(1)) if pt else "",
            "turl": _unescape(tu.group(1)) if tu else "",
        })
    return objects


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
    report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    size = size if size in _SIZES else "xga"
    ratio = ratio if ratio in _RATIOS else "w"
    url = build_search_url(query, size=size, ratio=ratio)
    page, relatorio = _fetch_report(url, timeout)
    if page is None:
        _finalizar_relatorio(relatorio, report, engine="google", objects=0, candidates=0)
        return []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    objetos = _google_result_objects(page)
    # Fase 14: as chaves tu/ou/ru/pt pertencem ao MESMO resultado. O parser
    # antigo montava quatro listas paralelas e associava por indice — qualquer
    # resultado sem uma das chaves deslocava todas as associacoes seguintes.
    for obj in objetos:
        direct = str(obj.get("murl") or "")
        if not direct or not _real_image_url(direct) or direct in seen:
            continue
        seen.add(direct)
        results.append(
            _candidate(
                query, f"{_SIZE_LABEL.get(size, size)}|{ratio}", direct,
                str(obj.get("purl") or ""), str(obj.get("t") or ""),
                str(obj.get("turl") or ""), engine="google",
                discovery_method="google_result",
            )
        )
        if len(results) >= limit:
            break
    _finalizar_relatorio(
        relatorio, report, engine="google",
        objects=len(objetos), candidates=len(results), html=page,
    )
    return results


# ---------------------------------------------------------------------------
# Yandex Images
# ---------------------------------------------------------------------------

_YANDEX_STATE_RE = re.compile(
    r'''\bdata-state\s*=\s*(?:"([^"\\]*(?:\\.[^"\\]*)*)"|'([^']*)')''',
    re.IGNORECASE | re.DOTALL,
)


def _public_http_url(url: str) -> bool:
    """Valida a forma da URL sem fazer DNS durante o parsing."""
    try:
        parsed = urlsplit(str(url or "").strip())
        host = parsed.hostname or ""
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if parsed.username or parsed.password:
        return False
    if host.casefold() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return bool(address.is_global)


def _yandex_img_url_links(page: str) -> list[str]:
    """Extrai ``img_url`` apenas de links pertencentes ao Yandex Images.

    ``parse_qs`` decodifica o parâmetro uma única vez. Isso é deliberado: a
    query string da imagem interna pode conter ``%`` e ``&`` próprios, e um
    segundo ``unquote`` corromperia essa URL.
    """
    encontrados: list[str] = []
    vistos: set[str] = set()
    for match in re.finditer(r'''\b(?:href|data-href)\s*=\s*(?:"([^"]+)"|'([^']+)')''', page, re.IGNORECASE):
        raw = html_lib.unescape(str(match.group(1) or match.group(2) or "")).strip()
        if not raw:
            continue
        href = urljoin(_YANDEX_IMAGES_BASE, raw)
        try:
            parsed = urlsplit(href)
            host = (parsed.hostname or "").casefold()
        except ValueError:
            continue
        if not (host == "yandex.com" or host.endswith(".yandex.com") or host == "yandex.ru" or host.endswith(".yandex.ru")):
            continue
        if "/images/" not in (parsed.path or ""):
            continue
        value = (parse_qs(parsed.query, keep_blank_values=True).get("img_url") or [""])[0]
        if not _public_http_url(value) or not _real_image_url(value) or value in vistos:
            continue
        vistos.add(value)
        encontrados.append(value)
    return encontrados


def _yandex_json_values(value: Any):
    """Percorre estruturas ``data-state`` sem depender de um schema único."""
    if isinstance(value, dict):
        yield value
        # ``viewerData``/``dups``/``preview`` são payload do próprio item,
        # não novos resultados. Não descê-los evita reemitir a mesma imagem
        # menor quando o item pai já foi processado.
        payload_keys = {"viewerData", "dups", "preview", "images"}
        for key, child in value.items():
            if key in payload_keys:
                continue
            yield from _yandex_json_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _yandex_json_values(child)


def _yandex_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip() if _public_http_url(value) else ""
    if isinstance(value, dict):
        for key in ("url", "href", "src", "original", "imageUrl"):
            candidate = _yandex_url(value.get(key))
            if candidate:
                return candidate
    return ""


def _yandex_result_objects(page: str) -> list[dict[str, Any]]:
    """Extrai imagem e origem do MESMO item moderno do Yandex.

    O parser é defensivo: se o provider alterar ``data-state``, nenhum URL é
    associado por posição entre listas independentes. O fallback ``img_url``
    continua sendo tratado separadamente como descoberta sem proveniência.
    """
    objetos: list[dict[str, Any]] = []
    vistos: set[str] = set()

    def _area(value: Any) -> int:
        if not isinstance(value, dict):
            return 0
        try:
            width = value.get("w") or value.get("width") or 0
            height = value.get("h") or value.get("height") or 0
            return max(0, int(width)) * max(0, int(height))
        except (TypeError, ValueError):
            return 0

    for match in _YANDEX_STATE_RE.finditer(page):
        bruto = match.group(1) or match.group(2) or ""
        bruto = html_lib.unescape(bruto)
        try:
            estado = json.loads(bruto)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for item in _yandex_json_values(estado):
            viewer = item.get("viewerData") if isinstance(item.get("viewerData"), dict) else {}
            snippet = viewer.get("snippet") if isinstance(viewer.get("snippet"), dict) else {}
            if not snippet and isinstance(item.get("snippet"), dict):
                snippet = item["snippet"]
            source = _yandex_url(snippet.get("url") or snippet.get("href"))
            title = str(snippet.get("title") or item.get("title") or "")
            thumbnail = _yandex_url(
                viewer.get("preview")
                or viewer.get("image")
                or item.get("preview")
                or item.get("image")
            )
            imagens: list[tuple[int, str]] = []
            for field in (viewer.get("dups"), viewer.get("preview"), item.get("dups"), item.get("images")):
                values = field if isinstance(field, list) else [field]
                for candidate in values:
                    direct = _yandex_url(candidate)
                    if not direct or not _real_image_url(direct):
                        continue
                    imagens.append((_area(candidate), direct))
            for _, direct in sorted(imagens, reverse=True):
                if direct in vistos:
                    continue
                vistos.add(direct)
                objetos.append({
                    "direct_image_url": direct,
                    "source_page_url": source,
                    "title": title,
                    "thumbnail_url": thumbnail,
                })
                break
    return objetos

def build_yandex_url(query: str) -> str:
    return f"{_YANDEX_IMAGES_BASE}?{urlencode({'text': query})}"


def search_yandex_images(
    query: str,
    *,
    size: str = "xga",
    ratio: str = "w",
    limit: int = 10,
    timeout: float = 30.0,
    report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    url = build_yandex_url(query)
    page, relatorio = _fetch_report(url, timeout)
    if page is None:
        _finalizar_relatorio(relatorio, report, engine="yandex", objects=0, candidates=0)
        return []
    objetos = _yandex_result_objects(page)
    img_urls = _yandex_img_url_links(page)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for objeto in objetos:
        direct = str(objeto.get("direct_image_url") or "")
        if not direct or not _real_image_url(direct) or direct in seen:
            continue
        seen.add(direct)
        results.append(_candidate(
            query, "1024x768|w", direct,
            str(objeto.get("source_page_url") or ""),
            str(objeto.get("title") or ""),
            str(objeto.get("thumbnail_url") or ""),
            engine="yandex", discovery_method="yandex_data_state",
        ))
        if len(results) >= limit:
            break
    for direct in img_urls:
        if direct in seen:
            continue
        seen.add(direct)
        # ``img_url`` representa a imagem pesquisada, não a sua origem.
        results.append(_candidate(
            query, "1024x768|w", direct, "", "", "", engine="yandex",
            discovery_method="yandex_img_url_param",
        ))
        if len(results) >= limit:
            break
    _finalizar_relatorio(
        relatorio, report, engine="yandex",
        objects=max(len(objetos), len(img_urls)), candidates=len(results), html=page,
    )
    return results


# ---------------------------------------------------------------------------
# Rotating facade
# ---------------------------------------------------------------------------

def _primary_engine(query: str) -> str:
    """Escolhe a engine primaria por hash estavel da query (CRC32).

    Alterna Bing/Yandex entre buscas diferentes (~50/50) mantendo a MESMA
    engine para a MESMA query (determinismo em retentativas). Google nunca e
    primaria: bloqueia IPs de datacenter, fica como fallback final.
    """
    return "yandex" if (zlib.crc32((query or "").encode("utf-8")) & 1) else "bing"



# --- Circuit breaker por engine (decisão registrada: NÃO tentar "vencer" o
# rate-limit com rotação de User-Agent). Estratégia: backoff com jitter, cache
# de resultados e cooldown da engine — o pipeline simplesmente usa as outras
# fontes enquanto uma está bloqueada. Estado em arquivo para sobreviver entre
# execuções do CLI (cada comando é um processo novo).
def _engine_state_path() -> Path:
    """Caminho do estado do breaker (lido a cada uso, não no import).

    Constante avaliada no import ignoraria ``UNICORNIO_ENGINE_STATE`` definido
    depois — o que fazia os testes compartilharem o arquivo real.
    """
    return Path(os.environ.get("UNICORNIO_ENGINE_STATE") or "/tmp/unicornio_media_engines.json")
_COOLDOWN_SEGUNDOS = 12 * 60   # após 3 falhas seguidas: 10-15 min fora
_BACKOFF_SEGUNDOS = (4.0, 20.0)  # 1ª falha: ~3-8 s · 2ª: 15-30 s (com jitter)


def _em_teste() -> bool:
    """Estamos num runner de testes (pytest OU unittest discover)?

    O CI oficial roda ``python -m unittest discover``, onde ``PYTEST_CURRENT_TEST``
    NÃO existe. Detectar só o pytest deixava o breaker ATIVO no CI: o estado em
    /tmp era compartilhado entre os casos, uma engine entrava em cooldown numa
    falha simulada e os testes seguintes dependiam da ORDEM — verde no pytest
    (com sleeps de backoff), vermelho no GitHub Actions.
    """
    if os.environ.get("UNICORNIO_TESTING"):
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return "unittest" in " ".join(sys.argv).lower()


def _breaker_ativo() -> bool:
    """O breaker fica desligado em runners de teste.

    Os testes dedicados do breaker/estado ligam explicitamente via
    ``UNICORNIO_ENGINE_STATE``.
    """
    if os.environ.get("UNICORNIO_ENGINE_STATE"):
        return True
    return not _em_teste()


def _ler_estado_engines() -> dict[str, dict[str, Any]]:
    try:
        dados = json.loads(_engine_state_path().read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except Exception:  # noqa: BLE001 - estado ausente/corrompido não bloqueia
        return {}


def _gravar_estado_engines(dados: dict[str, dict[str, Any]]) -> None:
    try:
        caminho = _engine_state_path()
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(json.dumps(dados), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def engine_disponivel(nome: str, *, agora: float | None = None) -> bool:
    """A engine não está em cooldown? (circuit breaker meio-aberto.)"""
    if not _breaker_ativo():
        return True
    estado = _ler_estado_engines().get(nome) or {}
    bloqueado_ate = float(estado.get("blocked_until") or 0)
    return (agora if agora is not None else time.time()) >= bloqueado_ate


def engine_falhou(nome: str) -> float:
    """Registra a falha e devolve quantos segundos a engine deve esperar.

    1ª falha -> backoff curto com jitter; a partir da 3ª, a engine entra em
    cooldown e o pipeline passa a usar as outras fontes (Bing cai, Yandex e
    Google continuam).
    """
    if not _breaker_ativo():
        return 0.0
    dados = _ler_estado_engines()
    atual = dados.get(nome) or {}
    falhas = int(atual.get("failures") or 0) + 1
    espera = 0.0
    if falhas >= 3:
        espera = _COOLDOWN_SEGUNDOS
        bloqueio = time.time() + espera
    else:
        base = _BACKOFF_SEGUNDOS[min(falhas, len(_BACKOFF_SEGUNDOS)) - 1]
        espera = base + random.uniform(0, base) * 0.6  # jitter (não é UA rotation)
        bloqueio = time.time() + espera
    dados[nome] = {"failures": falhas, "blocked_until": bloqueio,
                   "last_failure": time.time()}
    _gravar_estado_engines(dados)
    return espera


def engine_degradada(nome: str, kind: str) -> None:
    """Registra problema NÃO transitório (schema/JS/captcha) SEM cooldown.

    Cooldown existe para indisponibilidade temporária. Quando o motivo é
    permanente — o HTML mudou, a página exige JavaScript — entrar em cooldown só
    ESCONDE a engine: ela sai da ordem, o motivo não aparece em lugar nenhum e o
    time perde a fonte sem saber. Aqui o contador transitório é ZERADO (as "18
    falhas" do Google eram todas schema drift, não rate-limit) e o motivo fica no
    estado, visível em ``engines_status()`` e na telemetria.
    """
    if not _breaker_ativo():
        return
    dados = _ler_estado_engines()
    dados[nome] = {
        "failures": 0,
        "blocked_until": 0,
        "last_failure": time.time(),
        "failure_kind": kind,
        "non_transient": True,
    }
    _gravar_estado_engines(dados)


def engine_ok(nome: str) -> None:
    """Sucesso zera o contador (circuit breaker fechado)."""
    if not _breaker_ativo():
        return
    dados = _ler_estado_engines()
    if nome in dados:
        dados.pop(nome, None)
        _gravar_estado_engines(dados)


def engines_status() -> dict[str, dict[str, Any]]:
    """Estado atual das engines (para o funil/telemetria)."""
    return _ler_estado_engines()


def search_web_images(
    query: str,
    *,
    size: str = "xga",
    ratio: str = "w",
    limit: int = 10,
    timeout: float = 30.0,
    engine: str = "auto",
    accept: Any = None,
    remote_url_policy: str = "audit",
    audit: Any = None,
    reports: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Busca AGREGADA entre as engines (Fase 3), com parada por capacidade.

    Antes a primeira engine que retornasse QUALQUER coisa encerrava a busca
    ("first engine wins"). Se ela devolvesse 3 candidatos sem pagina de origem,
    a busca parava ali e o pipeline seguia com menos material do que a web
    oferecia — sem saber.

    Agora consulta as engines em ordem (a primaria alterna por hash estavel da
    query: ~50/50 Bing/Yandex; Google e fallback), acumula candidatos com
    dedupe por URL e PARA quando ja existem ``limit`` candidatos UTILIZAVEIS
    (com pagina de origem). Engine concreta usa apenas aquela.

    Fail-closed: sempre retorna lista (possivelmente vazia), nunca levanta.
    """
    query = (query or "").strip()
    if not query:
        return []
    if engine == "auto":
        # Google browser is attempted first. When Playwright/Chromium is not
        # installed or Google blocks the session, the existing deterministic
        # Bing/Yandex fallback remains active.
        # Keep the legacy HTML parser as a last-resort compatibility path for
        # environments where browser installation is still rolling out.
        order = ["google_browser", "bing", "yandex", "google"]
    else:
        order = [engine]
    from .google_browser import search_google_browser_images

    _fns = {
        "bing": search_bing_images,
        "yandex": search_yandex_images,
        "google": search_google_images,
        "google_browser": search_google_browser_images,
    }
    alvo = max(1, int(limit or 1))
    acumulado: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for name in order:
        breaker_name = provider_key(name)
        relatorio: dict[str, Any] = {}
        # Circuit breaker: engine em cooldown é simplesmente pulada — o
        # pipeline segue com as outras fontes (sem trocar User-Agent).
        if not engine_disponivel(breaker_name):
            relatorio = {
                "http_status": 0, "html_bytes": 0, "objects_parsed": 0, "candidates": 0,
                "failure_kind": "cooldown_skip", "parser_version": PARSER_VERSION,
            }
            if reports is not None:
                reports[name] = dict(relatorio)
            continue
        try:
            kwargs = {
                "size": size,
                "ratio": ratio,
                "limit": alvo,
                "timeout": timeout,
                "report": relatorio,
            }
            if name == "google_browser":
                kwargs.update(remote_url_policy=remote_url_policy, audit=audit)
            lote = _fns[name](query, **kwargs)
        except Exception:  # noqa: BLE001 - rotate on any failure
            lote = []
            relatorio.setdefault("failure_kind", "network_error")
        if reports is not None:
            reports[name] = dict(relatorio)
        if not lote:
            kind = str(relatorio.get("failure_kind") or "network_error")
            if kind not in FAILURE_KINDS_TRANSITORIOS:
                # Motivo PERMANENTE (schema mudou, página exige JS, captcha):
                # cooldown de 12 min aqui só esconderia a engine para sempre.
                # Registra o motivo e segue — ela continua na arquitetura.
                engine_degradada(breaker_name, kind)
                continue
            # Vazio/erro transitório conta como falha: backoff curto e, na 3ª,
            # cooldown da engine (Bing degradado não deve segurar o ciclo).
            espera = engine_falhou(breaker_name)
            if espera:
                try:
                    time.sleep(min(float(espera), 8.0))
                except Exception:  # noqa: BLE001 - sleep interrompido não quebra
                    pass
            continue
        engine_ok(breaker_name)
        novos: list[dict[str, Any]] = []
        for cand in lote or []:
            url = str(cand.get("direct_image_url") or "")
            if not url or url in vistos:
                continue
            vistos.add(url)
            cand.setdefault("engine", name)
            acumulado.append(cand)
            novos.append(cand)
        # Capacidade que encerra a busca: se o chamador fornece `accept`, o
        # critério é o número de candidatos ACEITOS (origem verificada +
        # subject + frame distinto) — nunca "usable" estrutural. Sem isso o
        # Bing podia devolver 6 resultados com source_page e todos serem lixo
        # de outra query: `usable=6` encerrava a busca e Google/Yandex, que
        # poderiam ter imagens boas, nunca eram consultados.
        if accept is not None:
            try:
                if int(accept(novos) or 0) >= alvo:
                    break
            except Exception:  # noqa: BLE001 - aceite é do chamador
                pass
        elif sum(1 for c in acumulado if c.get("usable")) >= alvo:
            break
    return acumulado


def search_web_images_batch(
    queries: list[str],
    *,
    size: str = "xga",
    ratio: str = "w",
    limit: int = 3,
    timeout: float = 30.0,
    engine: str = "auto",
    accept: Any = None,
    remote_url_policy: str = "audit",
    audit: Any = None,
) -> list[dict[str, Any]]:
    """Discover candidates for several distinct works concurrently.

    This is deliberately a *batch of exact queries*, not one broad query with
    all titles joined together. A broad query mixes franchises and makes it
    easy to attach the wrong artwork to a listicle item. Results preserve the
    input order and keep each work isolated while requiring only one CLI/tool
    interaction from the editorial agent.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for value in queries:
        query = str(value or "").strip()
        key = query.casefold()
        if query and key not in seen:
            seen.add(key)
            unique.append(query)
    if len(unique) > _MAX_BATCH_QUERIES:
        raise ValueError(f"media-search-listicle accepts at most {_MAX_BATCH_QUERIES} distinct titles")
    if not unique:
        return []

    def _search(query: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        # O callback do batch recebe TAMBÉM a query: cada item do listicle tem o
        # seu próprio subject e o aceite precisa ser medido por item (antes o
        # batch caía no critério antigo "usable" e encerrava a busca do item).
        def _accept_do_item(novos: list[dict[str, Any]]) -> int:
            return int(accept(novos, query) or 0) if accept is not None else 0

        relatorios: dict[str, dict[str, Any]] = {}
        candidatos = search_web_images(
            query, size=size, ratio=ratio, limit=limit, timeout=timeout,
            engine=engine,
            accept=_accept_do_item if accept is not None else None,
            remote_url_policy=remote_url_policy,
            audit=audit,
            reports=relatorios,
        )
        return candidatos, relatorios

    found: dict[str, list[dict[str, Any]]] = {}
    relatorios_por_query: dict[str, dict[str, dict[str, Any]]] = {}
    workers = min(_BATCH_WORKERS, len(unique))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_search, query): query for query in unique}
        for future in as_completed(futures):
            query = futures[future]
            try:
                candidatos, relatorios = future.result()
            except Exception:  # noqa: BLE001 - one failed engine must not lose the batch
                candidatos, relatorios = [], {}
            found[query] = candidatos
            relatorios_por_query[query] = relatorios
    return [
        {
            "query": query,
            "candidates": found.get(query, []),
            # Por que cada engine entregou (ou não entregou): sem isso uma engine
            # podia sumir do resultado sem deixar rastro na telemetria.
            "engine_reports": relatorios_por_query.get(query, {}),
        }
        for query in unique
    ]


__all__ = [
    "build_search_url", "build_bing_url", "build_yandex_url",
    "search_web_images", "search_web_images_batch", "search_bing_images", "search_google_images",
    "search_yandex_images",
    "reset_http_request_count", "http_request_count",
    # Saúde por engine (classificação da falha + relatório da tentativa)
    "PARSER_VERSION", "FAILURE_KINDS", "FAILURE_KINDS_TRANSITORIOS",
    "classify_failure", "provider_key", "engine_degradada", "engine_last_report", "engines_status",
]
