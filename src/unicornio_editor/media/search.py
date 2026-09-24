"""Discovery of image candidates via multiple search engines (Bing, Google, Yandex).

Deterministic and read-only: builds the search URL for each engine and returns
image candidates so the LLM does not have to reason about search URLs or parse
results - that cost is moved to code (token economy).

Google Images blocks datacenter/cloud IPs (CAPTCHA/Cloudflare), so in production
it frequently returns an empty/unparseable page. To stay robust and diversify
sources, ``search_web_images`` alternates the PRIMARY engine by query hash
(~50/50 Bing / Yandex) and rotates to the other, then Google, on failure:

  1. Bing Images  or  Yandex Images  (primary, alternates by query)
  2. the other of the two (fallback)
  3. Google Images (last resort - index only, never the source)

IMPORTANT policy: the search engine is only a DISCOVERY INDEX, never the source.
The direct_image_url returned is the real image URL found in the result
metadata (the page's own image, not the engine's preview thumbnail). The agent
must still open source_page_url and confirm the image is listed there
(verify_downloaded_against_source in the apply) and register credit.
"""

from __future__ import annotations

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



def _fetch(url: str, timeout: float) -> str:
    global _HTTP_REQUESTS
    with _HTTP_REQUESTS_LOCK:
        _HTTP_REQUESTS += 1
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


def _valid_http(url: str) -> bool:
    """URL http(s) absoluta com host (Fase 4)."""
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _candidate(query, size_filter, direct, page, title, thumb, *, engine=""):
    """Candidato normalizado — com `usable` explicito (Fase 4).

    A cadeia verificavel e query -> pagina de origem -> URL da imagem -> bytes.
    Sem `source_page_url` nao existe cadeia: o candidato do Yandex (que entrega
    a imagem sem a pagina) fica `usable=False` + `discovery_only`, e nunca deve
    entrar no media_plan. Antes ele entrava com source vazio e so era
    descoberto no apply (depois de baixar e gastar upload).
    """
    page_clean = _clean_page_url(page)
    usable = _valid_http(direct) and _valid_http(page_clean)
    motivo = ""
    if not usable:
        if not _valid_http(page_clean):
            motivo = "missing_source_page"
        else:
            motivo = "invalid_direct_image_url"
    return {
        "query": query,
        "size_filter": size_filter,
        "title": (title or "")[:200],
        "direct_image_url": direct,
        "source_page_url": page_clean,
        "thumbnail_url": thumb,
        "engine": engine,
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
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Fase 14: cada resultado do Bing carrega o proprio JSON no atributo `m`
    # (murl+purl+turl do MESMO resultado). O parser antigo coletava as tres
    # listas separadamente e associava por INDICE: quando um resultado nao
    # tinha purl (ou a ordem divergia), a imagem era ligada a pagina de OUTRO
    # resultado — origem errada, verificacao de origem inutil.
    for obj in _bing_result_objects(html):
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
            )
        )
        if len(results) >= limit:
            break
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
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Fase 14: as chaves tu/ou/ru/pt pertencem ao MESMO resultado. O parser
    # antigo montava quatro listas paralelas e associava por indice — qualquer
    # resultado sem uma das chaves deslocava todas as associacoes seguintes.
    for obj in _google_result_objects(page):
        direct = str(obj.get("murl") or "")
        if not direct or not _real_image_url(direct) or direct in seen:
            continue
        seen.add(direct)
        results.append(
            _candidate(
                query, f"{_SIZE_LABEL.get(size, size)}|{ratio}", direct,
                str(obj.get("purl") or ""), str(obj.get("t") or ""),
                str(obj.get("turl") or ""), engine="google",
            )
        )
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
        # Fase 4: o Yandex entrega a imagem SEM a pagina de origem. O
        # candidato nasce `discovery_only` (usable=False) — serve de pista,
        # nunca entra no media_plan.
        results.append(_candidate(query, "1024x768|w", direct, "", "", "", engine="yandex"))
        if len(results) >= limit:
            break
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
        first = _primary_engine(query)
        second = "yandex" if first == "bing" else "bing"
        order = [first, second, "google"]
    else:
        order = [engine]
    _fns = {
        "bing": search_bing_images,
        "yandex": search_yandex_images,
        "google": search_google_images,
    }
    alvo = max(1, int(limit or 1))
    acumulado: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for name in order:
        # Circuit breaker: engine em cooldown é simplesmente pulada — o
        # pipeline segue com as outras fontes (sem trocar User-Agent).
        if not engine_disponivel(name):
            continue
        try:
            lote = _fns[name](query, size=size, ratio=ratio, limit=alvo, timeout=timeout)
        except Exception:  # noqa: BLE001 - rotate on any failure
            lote = []
        if not lote:
            # Vazio/erro conta como falha: backoff curto e, na 3ª, cooldown da
            # engine (Bing degradado não deve segurar o ciclo).
            espera = engine_falhou(name)
            if espera:
                try:
                    time.sleep(min(float(espera), 8.0))
                except Exception:  # noqa: BLE001 - sleep interrompido não quebra
                    pass
            continue
        engine_ok(name)
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

    def _search(query: str) -> list[dict[str, Any]]:
        # O callback do batch recebe TAMBÉM a query: cada item do listicle tem o
        # seu próprio subject e o aceite precisa ser medido por item (antes o
        # batch caía no critério antigo "usable" e encerrava a busca do item).
        def _accept_do_item(novos: list[dict[str, Any]]) -> int:
            return int(accept(novos, query) or 0) if accept is not None else 0

        return search_web_images(
            query, size=size, ratio=ratio, limit=limit, timeout=timeout,
            engine=engine,
            accept=_accept_do_item if accept is not None else None,
        )

    found: dict[str, list[dict[str, Any]]] = {}
    workers = min(_BATCH_WORKERS, len(unique))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_search, query): query for query in unique}
        for future in as_completed(futures):
            query = futures[future]
            try:
                found[query] = future.result()
            except Exception:  # noqa: BLE001 - one failed engine must not lose the batch
                found[query] = []
    return [{"query": query, "candidates": found.get(query, [])} for query in unique]


__all__ = [
    "build_search_url", "build_bing_url", "build_yandex_url",
    "search_web_images", "search_web_images_batch", "search_bing_images", "search_google_images",
    "search_yandex_images",
    "reset_http_request_count", "http_request_count",
]
