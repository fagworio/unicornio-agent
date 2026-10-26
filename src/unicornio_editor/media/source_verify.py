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
import io
import json
import re
from http.client import InvalidURL
from pathlib import Path
from time import monotonic
from typing import Any
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse
from urllib.request import Request
from .url_safety import urlopen_remote
from .page_assets import extract_page_assets, is_noise_image_url, rank_page_assets
from .phash_policy import PHASH_NEAR_EXACT_MAX_DISTANCE

_PAGE_MAX_BYTES = 2 * 1024 * 1024
_IMG_MAX_BYTES = 8 * 1024 * 1024
_VERIFY_TOTAL_MAX_BYTES = 32 * 1024 * 1024
_MAX_DOWNLOADS = 6
_MAX_SOURCE_ASSET_COMPARISONS = 12
_FETCH_TIMEOUT = 15
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_IMG_EXT = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|bmp)(?:\?|#|$)", re.IGNORECASE)

SOURCE_VERIFICATION_RETRYABLE_CODES = frozenset({
    "source_page_unavailable",
    "source_asset_unavailable",
    "source_scan_incomplete",
    "source_decode_failed",
    "candidate_asset_unavailable",
})


def _coded_reason(code: str, detail: str) -> str:
    """Keep a stable machine reason while retaining a useful audit detail."""
    return f"{code}: {detail}"


def verification_result(ok: bool, reason: str) -> dict[str, Any]:
    """Normalize the legacy ``(ok, reason)`` result for structured callers."""
    raw = str(reason or "")
    code, separator, detail = raw.partition(":")
    reason_code = code.strip() if separator and code.strip() else (
        "source_verified" if ok else "source_mismatch"
    )
    return {
        "ok": bool(ok),
        "reason_code": reason_code,
        "reason": raw,
        "detail": detail.strip() if separator else raw,
        "recoverable": reason_code in SOURCE_VERIFICATION_RETRYABLE_CODES,
        # This helper is used after the downloaded candidate was compared to
        # assets read from the source page.  A mere EXACT_URL/SLUG_MATCH from
        # the HTML preflight never reaches this path and is not proof of bytes.
        "source_comparison_proved": reason_code in {
            "source_reference_found", "source_mismatch", "source_bytes_mismatch"
        },
    }


def discover_article_source_candidates(
    page_url: str,
    *,
    subject: str = "",
    subjects: list[str] | tuple[str, ...] | None = None,
    limit: int = 32,
    audit=None,
    stats: dict[str, int] | None = None,
    url_policy: str = "audit",
    is_listicle: bool | None = None,
) -> list[dict[str, Any]]:
    """Extract candidates directly from the article's ``Fonte`` page.

    This is intentionally only HTTP/parser discovery. The normal V2
    media-plan gates still validate and execute every candidate.
    """
    if not _valid_http(page_url):
        return []
    budget = [_VERIFY_TOTAL_MAX_BYTES]
    payload = _fetch(page_url, "text/html", _PAGE_MAX_BYTES, budget, audit, url_policy)
    if payload is None:
        return []
    html = payload.decode("utf-8", "ignore")
    if is_listicle is None:
        from ..list_quality import extract_list_items

        is_listicle = len(extract_list_items("", html)) >= 2
    editorial_subjects = []
    for value in (subjects or (subject,)):
        normalized = " ".join(str(value or "").split()).strip()
        if normalized and normalized.casefold() not in {item.casefold() for item in editorial_subjects}:
            editorial_subjects.append(normalized)
    primary_subject = editorial_subjects[0] if editorial_subjects else subject

    def subject_for_asset(asset) -> str:
        if len(editorial_subjects) <= 1:
            return primary_subject
        if not is_listicle:
            # Ordinary articles can mention secondary entities without turning
            # each image into a list item. Keep the article's primary identity;
            # local evidence is still scored below and the normal gates remain.
            return primary_subject
        context = " ".join((asset.heading, asset.alt, asset.figcaption)).casefold()
        context_tokens = set(re.findall(r"[a-z0-9]+", context))
        matches = []
        for candidate_subject in editorial_subjects:
            tokens = [token for token in re.findall(r"[a-z0-9]+", candidate_subject.casefold()) if len(token) >= 3]
            if tokens and all(token in context_tokens for token in tokens):
                matches.append(candidate_subject)
        return matches[0] if len(matches) == 1 else ""
    extracted_assets = extract_page_assets(html, page_url)
    editorial_assets = [
        asset for asset in extracted_assets
        if asset.context_kind in {"article_body", "article_header", "metadata"}
    ]
    if stats is not None:
        stats.update({
            "raw_assets": len(extracted_assets),
            "editorial_assets": len(editorial_assets),
            "filtered_assets": len(extracted_assets) - len(editorial_assets),
        })
    assets = rank_page_assets(
        editorial_assets,
        primary_subject or page_url,
        subject=" ".join(editorial_subjects),
        limit=max(1, int(limit)),
    )
    generic = {
        "logo", "avatar", "favicon", "icon", "pixel", "tracking", "spacer",
        "audio", "podcast", "soundcloud", "spotify", "related",
        "recommend", "sidebar", "author",
    }
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in assets:
        url = str(asset.url or "").strip()
        if not _valid_http(url) or is_noise_image_url(url) or url in seen:
            continue
        seen.add(url)
        if asset.context_kind == "excluded":
            continue
        asset_subject = subject_for_asset(asset)
        if is_listicle and len(editorial_subjects) > 1 and not asset_subject:
            # An asset from a listicle source without an unambiguous local
            # heading/alt/caption must not inherit item one's identity.
            continue
        parsed = urlparse(unquote(url))
        stem = re.sub(r"\.[a-z0-9]{2,5}$", "", parsed.path.rsplit("/", 1)[-1], flags=re.I)
        if generic.intersection(set(re.split(r"[-_. ]+", stem.casefold()))):
            continue
        try:
            width = int(asset.width or 0)
            height = int(asset.height or 0)
        except ValueError:
            width = height = 0
        if (width and width < 180) or (height and height < 100):
            continue
        candidate_id = hashlib.sha256(
            f"article_source|{page_url}|{url}".encode("utf-8", "ignore")
        ).hexdigest()[:20]
        from .evidence import evidence_score, source_context

        context = source_context(html, url, base_url=page_url)
        context.update({
            "alt_original": asset.alt,
            "figcaption": asset.figcaption,
            "heading": asset.heading,
            "filename": stem,
        })
        evidence = evidence_score(
            asset_subject,
            filename=context.get("filename", ""),
            og_title=context.get("og_title", ""),
            page_title=context.get("page_title", ""),
            alt_original=context.get("alt_original", ""),
            figcaption=context.get("figcaption", ""),
            heading=context.get("heading", ""),
            page_url=page_url,
            source_page_present=True,
            image_in_source=True,
        )
        result.append({
            "candidate_id": candidate_id,
            "discovery_image_url": url,
            "direct_image_url": url,
            "source_page_url": page_url,
            "source_origin_type": "article_source",
            "origin_type": "article_source",
            "source_verified": True,
            "provenance_verified": True,
            "source_verification": "verified_by_extraction",
            "usable": True,
            "discovery_only": False,
            "engine": "article_source",
            "query": "article_source",
            "evidence": evidence,
            "evidence_score": int(evidence.get("score") or 0),
            "needs_vision": bool(evidence.get("needs_vision")),
            "valid": True,
            "verification_level": "ARTICLE_SOURCE",
            "alt_text": asset.alt or asset.figcaption or asset.heading or asset_subject,
            "credit_text": f"Crédito da imagem: {(parsed.hostname or 'fonte original').lower()}",
            "subject": asset_subject,
            "subjects": list(editorial_subjects),
            "source_context_kind": asset.context_kind,
            "role": "inline",
        })
    return result


def enrich_wikimedia_rights(candidate: dict[str, Any], *, audit=None, url_policy: str = "audit") -> dict[str, Any]:
    """Attach rights evidence from Wikimedia Commons' authoritative API.

    This never infers permission from a search result. It only enriches a
    candidate whose source page is a Commons file page and whose exact direct
    URL is returned by ``imageinfo`` together with compatible extmetadata.
    """
    source = str(candidate.get("source_page_url") or "").strip()
    host = (urlparse(source).hostname or "").casefold()
    if host not in {"commons.wikimedia.org", "www.commons.wikimedia.org"}:
        return candidate
    marker = "/wiki/"
    if marker not in urlparse(source).path:
        return candidate
    title = unquote(urlparse(source).path.split(marker, 1)[1])
    if not title or title.casefold().startswith("special:"):
        return candidate
    api = "https://commons.wikimedia.org/w/api.php?action=query&format=json&titles=" + quote(title, safe="") + "&prop=imageinfo&iiprop=url|extmetadata"
    payload = _fetch(api, "application/json", _PAGE_MAX_BYTES, [_VERIFY_TOTAL_MAX_BYTES], audit, url_policy)
    if not payload:
        return candidate
    try:
        pages = (json.loads(payload.decode("utf-8", "ignore")).get("query") or {}).get("pages") or {}
        page = next(iter(pages.values()))
        info = (page.get("imageinfo") or [])[0]
        metadata = info.get("extmetadata") or {}
        def value(name: str) -> str:
            item = metadata.get(name) or {}
            return str(item.get("value") if isinstance(item, dict) else item or "").strip()
        license_name = value("LicenseShortName")
        license_url = value("LicenseUrl")
        author = re.sub(r"<[^>]+>", " ", value("Artist"))
        author = " ".join(author.split())
        direct = str(info.get("url") or "").strip()
        if not direct or not license_name or not license_url or not author:
            return candidate
        discovered = str(candidate.get("direct_image_url") or "").strip()
        if discovered and discovered != direct:
            budget = [_VERIFY_TOTAL_MAX_BYTES]
            discovered_bytes = _fetch(discovered, "image/*", _IMG_MAX_BYTES, budget, audit, url_policy)
            authorized_bytes = _fetch(direct, "image/*", _IMG_MAX_BYTES, budget, audit, url_policy)
            if not discovered_bytes or not authorized_bytes:
                return candidate
            equivalent, _identity_reason = verify_image_identity(discovered_bytes, authorized_bytes)
            if not equivalent:
                return candidate
        enriched = dict(candidate)
        enriched.update({
            "source_page_url": source,
            "direct_image_url": direct,
            "author": author,
            "license": license_name,
            "license_url": license_url,
            "captured_at": __import__("datetime").date.today().isoformat(),
            "credit_text": f"Crédito da imagem: {author}. Licença {license_name}.",
            "alt_text": str(candidate.get("alt_text") or candidate.get("subject") or title),
            "rights_evidence": {
                "provider": "wikimedia_commons_api",
                "file_title": title,
                "image_url": direct,
                "license": license_name,
                "license_url": license_url,
                "author": author,
            },
        })
        from .license import attest_wikimedia_rights
        attestation = attest_wikimedia_rights(enriched)
        if attestation:
            enriched["source_rights_attestation"] = attestation
            enriched["rights_verified"] = True
            return enriched
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return candidate
    return candidate


def _fetch(
    url: str,
    accept: str,
    max_bytes: int,
    budget: list[int],
    audit=None,
    url_policy: str = "audit",
) -> bytes | None:
    if budget[0] <= 0:
        return None
    try:
        request = Request(
            url,
            headers={"User-Agent": _UA, "Accept": accept},
        )
        with urlopen_remote(
            request, timeout=_FETCH_TIMEOUT, mode=url_policy, audit=audit
        ) as response:
            data = response.read(min(max_bytes, budget[0]) + 1)
            if len(data) > max_bytes:
                return None
            budget[0] -= len(data)
            return data
    except (HTTPError, URLError, OSError, ValueError, InvalidURL):
        return None


def _slug(url: str) -> str:
    """Nome base do arquivo (sem extensao/query) para casar variantes."""
    path = urlparse(url).path
    base = unquote(path.rsplit("/", 1)[-1])
    return re.sub(r"\.(?:jpe?g|png|webp|gif|avif|bmp)$", "", base, flags=re.I).lower()


def _asset_rendition_key(url: str) -> str:
    """Key common CDN resize variants for later byte/visual verification.

    The key only prioritizes a page-declared asset. It is never itself a proof
    of provenance, so a matching rendition must still pass the byte/pHash
    comparison below.
    """
    parsed = urlparse(unquote(str(url or "")))
    host = (parsed.hostname or "").casefold()
    stem = _slug(url)
    stem = re.sub(r"(?:[-_.](?:\d{2,5}x\d{2,5}|(?:w|h|width|height)[-_]?\d{2,5}))+$", "", stem)
    return f"{host}|{stem}" if host and stem else ""


def _image_urls_in_page(html: str, base_url: str) -> list[str]:
    """URLs de assets declarados pela página, inclusive CDNs sem extensão."""
    return [asset.url for asset in extract_page_assets(html, base_url)]


def source_page_html(
    page_url: str,
    *,
    cache: dict[str, str | None] | None = None,
    cache_lock: Lock | None = None,
    audit=None,
    url_policy: str = "audit",
) -> str | None:
    """Fetch and cache source HTML for independent rights-evidence checks."""
    if not _valid_http(page_url):
        return None
    cache = cache if cache is not None else {}

    def populate() -> None:
        if page_url in cache:
            return
        budget = [_VERIFY_TOTAL_MAX_BYTES]
        payload = _fetch(page_url, "text/html", _PAGE_MAX_BYTES, budget, audit, url_policy)
        cache[page_url] = payload.decode("utf-8", "ignore") if payload is not None else None

    if cache_lock is None:
        populate()
    else:
        with cache_lock:
            populate()
    return cache[page_url]


def _normalized_url(url: str) -> str:
    """Normalize harmless URL differences for exact source-page matching."""
    parsed = urlparse(unquote(url.strip()))
    encoded_path = quote(parsed.path, safe="/!$&'()*+,;=:@-._~")
    encoded_query = quote(parsed.query, safe="/?=&!$&'()*+,;:@-._~")
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), encoded_path, "", encoded_query, "")
    )


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def verify_image_identity(
    downloaded: bytes,
    source: bytes,
    *,
    phash_threshold: int = PHASH_NEAR_EXACT_MAX_DISTANCE,
) -> tuple[bool, str]:
    """Classifica bytes diferentes que ainda representam o mesmo frame.

    Proveniência continua exigindo que ``source`` seja um asset listado na
    página. Depois dessa prova, recompressão/resize legítimos são aceitos por
    identidade visual conservadora; imagem diferente continua rejeitada.
    """
    try:
        from PIL import Image, ImageChops, ImageStat
        import imagehash

        with Image.open(io.BytesIO(downloaded)) as d_img, Image.open(io.BytesIO(source)) as s_img:
            d = d_img.convert("RGB")
            s = s_img.convert("RGB")
            if not d.width or not d.height or not s.width or not s.height:
                return False, "MISMATCH"
            d_ratio = d.width / d.height
            s_ratio = s.width / s.height
            if abs(d_ratio - s_ratio) > 0.02:
                return False, "MISMATCH"
            d_mean = ImageStat.Stat(d.resize((32, 32))).mean
            s_mean = ImageStat.Stat(s.resize((32, 32))).mean
            if sum(abs(a - b) for a, b in zip(d_mean, s_mean)) > 30:
                return False, "MISMATCH"
            if d.size == s.size and ImageChops.difference(d, s).getbbox() is None:
                return True, "PIXEL_IDENTICAL"
            dh = imagehash.phash(d)
            sh = imagehash.phash(s)
            if int(dh - sh) <= phash_threshold:
                return True, "VISUAL_VARIANT"
    except Exception:  # noqa: BLE001 - imagem inválida é fail-closed
        return False, "UNVERIFIED"
    return False, "MISMATCH"


def _valid_http(url: str) -> bool:
    """URL HTTP(S) absoluta, sem caracteres de controle ou espaço cru."""
    raw = str(url or "")
    if any(char.isspace() or ord(char) < 0x20 for char in raw):
        return False
    try:
        parsed = urlparse(raw)
        # Accessing port validates malformed bracketed/port syntax as well.
        _ = parsed.port
    except (ValueError, TypeError):
        return False
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
    )


def _validate_discovered_candidate(
    candidate: dict[str, Any],
    *,
    cache: dict[str, list[str] | None] | None = None,
    cache_html: dict[str, str] | None = None,
    subject: str = "",
    audit=None,
    url_policy: str = "audit",
) -> dict[str, Any]:
    """Valida um candidato ANTES do media_plan (Fase 5).

    Checagem LEVE e determinística — sem baixar bytes: a ``direct_image_url``
    aparece mesmo na ``source_page_url``? Antes isso só era descoberto no
    apply, DEPOIS do download e do upload: a Media Library enchia de imagens
    que nunca passariam e o post entrava em rework.

    Retorna ``{"valid", "source_verified", "image_url", "source_page_url",
    "reason", "images_in_page"}``. Só ``valid=True`` deve seguir para o plano.
    O confronto byte-a-byte continua no apply
    (``verify_downloaded_against_source``), que é a prova final.
    """
    image_url = str(candidate.get("direct_image_url") or "").strip()
    page_url = str(candidate.get("source_page_url") or "").strip()
    resultado: dict[str, Any] = {
        "valid": False,
        "source_verified": False,
        "image_url": image_url,
        "source_page_url": page_url,
        "reason": "",
        "reason_code": "",
        "recoverable": False,
        "source_comparison_proved": False,
        "images_in_page": 0,
        "verification_status": "unverified",
        "verification_stage": "source_page_fetch",
        "verification_stage_durations_ms": {},
        "comparisons_attempted": 0,
        "assets_considered": 0,
    }
    page_validation_started = monotonic()
    if not _valid_http(page_url):
        resultado["verification_stage"] = "source_page_validation"
        resultado["verification_stage_durations_ms"]["source_page_validation"] = round(
            (monotonic() - page_validation_started) * 1000, 1
        )
        resultado["reason_code"] = "source_page_unavailable"
        resultado["recoverable"] = True
        resultado["reason"] = _coded_reason(
            "source_page_unavailable", "source_page_url ausente ou invalida"
        )
        return resultado
    candidate_validation_started = monotonic()
    if not _valid_http(image_url):
        resultado["verification_stage"] = "candidate_url_validation"
        resultado["verification_stage_durations_ms"]["source_page_validation"] = round(
            (monotonic() - page_validation_started) * 1000, 1
        )
        resultado["verification_stage_durations_ms"]["candidate_url_validation"] = round(
            (monotonic() - candidate_validation_started) * 1000, 1
        )
        resultado["reason_code"] = "source_asset_unavailable"
        resultado["recoverable"] = True
        resultado["reason"] = _coded_reason(
            "source_asset_unavailable", "direct_image_url ausente ou invalida"
        )
        return resultado

    cache = cache if cache is not None else {}
    budget = [_VERIFY_TOTAL_MAX_BYTES]
    if page_url not in cache:
        fetch_started = monotonic()
        pagina = _fetch(page_url, "text/html", _PAGE_MAX_BYTES, budget, audit, url_policy)
        resultado["verification_stage_durations_ms"]["source_page_fetch"] = round(
            (monotonic() - fetch_started) * 1000, 1
        )
        texto = pagina.decode("utf-8", "ignore") if pagina is not None else ""
        extraction_started = monotonic()
        cache[page_url] = _image_urls_in_page(texto, page_url) if pagina is not None else None
        if pagina is not None:
            resultado["verification_stage_durations_ms"]["asset_extraction"] = round(
                (monotonic() - extraction_started) * 1000, 1
            )
        # Guarda o HTML da origem: o chamador extrai o contexto (og:title,
        # page title, alt original, figcaption) e alimenta o score de
        # relevância sem baixar a página uma segunda vez.
        if cache_html is not None and texto:
            cache_html[page_url] = texto
    listadas = cache[page_url]
    if listadas is None:
        resultado["reason_code"] = "source_page_unavailable"
        resultado["recoverable"] = True
        resultado["reason"] = _coded_reason(
            "source_page_unavailable",
            "pagina de origem inacessivel; nao e possivel confirmar a imagem",
        )
        return resultado
    if not listadas:
        resultado["verification_stage"] = "asset_extraction"
        resultado["reason_code"] = "source_scan_incomplete"
        resultado["recoverable"] = True
        resultado["reason"] = _coded_reason(
            "source_scan_incomplete",
            "pagina acessivel, mas o parser nao encontrou referencias de imagem",
        )
        return resultado
    resultado["images_in_page"] = len(listadas)

    reference_started = monotonic()
    alvo = _normalized_url(image_url)
    exact_match = next((url for url in listadas if _normalized_url(url) == alvo), "")
    if exact_match:
        resultado["verification_stage_durations_ms"]["source_reference"] = round(
            (monotonic() - reference_started) * 1000, 1
        )
        resultado["verification_stage"] = "source_reference"
        resultado["valid"] = True
        resultado["source_verified"] = True
        resultado["matched_image_url"] = exact_match
        resultado["verification_level"] = "EXACT_URL"
        resultado["verification_status"] = "verified"
        resultado["reason_code"] = "source_reference_found"
        resultado["reason"] = _coded_reason(
            "source_reference_found", "URL exata encontrada no HTML da pagina de origem"
        )
        return resultado
    slug = _slug(image_url)
    if slug:
        slug_match = next((url for url in listadas if _slug(url) == slug), "")
    else:
        slug_match = ""
    if slug_match:
        resultado["verification_stage_durations_ms"]["source_reference"] = round(
            (monotonic() - reference_started) * 1000, 1
        )
        resultado["verification_stage"] = "source_reference"
        resultado["valid"] = True
        resultado["source_verified"] = True
        resultado["matched_image_url"] = slug_match
        resultado["verification_level"] = "SLUG_MATCH"
        resultado["verification_status"] = "verified"
        resultado["reason_code"] = "source_reference_found"
        resultado["reason"] = _coded_reason(
            "source_reference_found", "mesma imagem (slug) listada na pagina de origem"
        )
        return resultado
    resultado["verification_stage_durations_ms"]["source_reference"] = round(
        (monotonic() - reference_started) * 1000, 1
    )

    rendition_key = _asset_rendition_key(image_url)
    rendition_matches = [
        url for url in listadas
        if rendition_key and _asset_rendition_key(url) == rendition_key
    ]

    # Candidatos de engines como Yandex podem apontar para um CDN diferente do
    # asset listado pela página. A origem continua sendo obrigatória: baixamos o
    # candidato e os assets listados e promovemos somente o correspondente
    # visual conservador para a URL canônica da página.
    page_html = (cache_html or {}).get(page_url, "")
    if page_html:
        ranked_assets = rank_page_assets(
            extract_page_assets(page_html, page_url), image_url, subject=subject, limit=10
        )
        ranked_urls = [*rendition_matches, *(asset.url for asset in ranked_assets)]
        # Keep any parser/HTML representation not understood by PageAsset in
        # the bounded list, without reverting to the old first-six policy.
        ranked_urls.extend(url for url in listadas if url not in ranked_urls)
    else:
        ranked_urls = [*rendition_matches, *list(listadas)]
    ranked_urls = list(dict.fromkeys(ranked_urls))
    resultado["assets_considered"] = len(ranked_urls)
    scan_limit = max(_MAX_DOWNLOADS, _MAX_SOURCE_ASSET_COMPARISONS)
    scan_urls = ranked_urls[:scan_limit]
    scan_truncated = len(ranked_urls) > len(scan_urls)
    try:
        resultado["verification_stage"] = "candidate_image_fetch"
        candidate_fetch_started = monotonic()
        candidate_bytes = _fetch(image_url, "image/*", _IMG_MAX_BYTES, budget, audit, url_policy)
        resultado["verification_stage_durations_ms"]["candidate_image_fetch"] = round(
            (monotonic() - candidate_fetch_started) * 1000, 1
        )
        if candidate_bytes is None:
            resultado.update({
                "reason_code": "candidate_asset_unavailable",
                "recoverable": True,
                "reason": _coded_reason(
                    "candidate_asset_unavailable",
                    "bytes da candidata indisponíveis para comparação com a origem",
                ),
            })
            return resultado
        resultado["verification_stage"] = "visual_comparison"
        compared = 0
        unavailable = 0
        decode_failed = False
        for listed_url in scan_urls:
            source_fetch_started = monotonic()
            asset_bytes = _fetch(listed_url, "image/*", _IMG_MAX_BYTES, budget, audit, url_policy)
            resultado["verification_stage_durations_ms"]["source_asset_fetch"] = round(
                resultado["verification_stage_durations_ms"].get("source_asset_fetch", 0.0)
                + (monotonic() - source_fetch_started) * 1000,
                1,
            )
            if asset_bytes is None:
                unavailable += 1
                resultado["verification_stage"] = "source_asset_fetch"
                continue
            resultado["verification_stage"] = "visual_comparison"
            visual_started = monotonic()
            same_frame, level = verify_image_identity(candidate_bytes, asset_bytes)
            resultado["verification_stage_durations_ms"]["visual_comparison"] = round(
                resultado["verification_stage_durations_ms"].get("visual_comparison", 0.0)
                + (monotonic() - visual_started) * 1000,
                1,
            )
            resultado["comparisons_attempted"] += 1
            if level == "UNVERIFIED":
                decode_failed = True
                continue
            compared += 1
            if same_frame:
                resultado["valid"] = True
                resultado["source_verified"] = True
                resultado["matched_image_url"] = listed_url
                resultado["verification_level"] = level
                resultado["verification_status"] = "verified"
                resultado["source_comparison_proved"] = True
                resultado["reason_code"] = "source_reference_found"
                resultado["reason"] = "asset visualmente correspondente encontrado na pagina de origem"
                return resultado
    except Exception:  # noqa: BLE001 - falha de rede/formato permanece fail-closed
        resultado.update({
            "reason_code": "source_scan_incomplete",
            "recoverable": True,
            "reason": _coded_reason(
                "source_scan_incomplete", "falha técnica durante a comparação de assets"
            ),
        })
        return resultado
    if scan_truncated:
        code, detail = "source_scan_incomplete", "a origem excede o limite de assets comparados"
        resultado["verification_stage"] = "asset_comparison_limit"
    elif unavailable:
        code, detail = "source_asset_unavailable", "um ou mais assets da origem não puderam ser baixados"
        resultado["verification_stage"] = "source_asset_fetch"
    elif decode_failed:
        code, detail = "source_decode_failed", "um ou mais assets da origem não puderam ser decodificados"
        resultado["verification_stage"] = "visual_comparison"
    elif compared:
        code, detail = "source_mismatch", "comparação completa comprovou bytes visuais incompatíveis"
        resultado["verification_stage"] = "visual_comparison"
        resultado["verification_status"] = "incompatible"
        resultado["source_comparison_proved"] = True
    else:
        code, detail = "source_scan_incomplete", "não foi possível comparar assets da origem"
        resultado["verification_stage"] = "visual_comparison"
    resultado["reason_code"] = code
    resultado["recoverable"] = code in SOURCE_VERIFICATION_RETRYABLE_CODES
    resultado["reason"] = _coded_reason(code, detail)
    return resultado


def validate_discovered_candidate(
    candidate: dict[str, Any],
    *,
    cache: dict[str, list[str] | None] | None = None,
    cache_html: dict[str, str] | None = None,
    subject: str = "",
    audit=None,
    url_policy: str = "audit",
) -> dict[str, Any]:
    """Validate source membership and report bounded stage timing.

    Timing is diagnostic only; it never affects a provenance decision or a
    retry policy. The wrapper also keeps every early-return path observable.
    """
    started = monotonic()
    result = _validate_discovered_candidate(
        candidate,
        cache=cache,
        cache_html=cache_html,
        subject=subject,
        audit=audit,
        url_policy=url_policy,
    )
    result["verification_elapsed_ms"] = max(
        0.0, round((monotonic() - started) * 1000, 1)
    )
    return result


def verify_downloaded_against_source(
    *,
    source_page_url: str,
    downloaded: Path,
    direct_image_url: str,
    cache: dict[str, list[str] | None] | None = None,
    cache_html: dict[str, str | None] | None = None,
    cache_lock: Lock | None = None,
    audit=None,
    url_policy: str = "audit",
) -> tuple[bool, str]:
    """Confirma que ``downloaded`` corresponde a uma imagem listada na pagina.

    Returns ``(ok, reason)``. Fail-closed: pagina inacessivel, imagem nao
    listada, ou bytes divergentes -> ``(False, motivo)``.
    """
    if not isinstance(source_page_url, str) or not source_page_url.strip():
        return False, _coded_reason(
            "source_page_unavailable",
            "source_page_url ausente; impossivel verificar a imagem contra a origem",
        )
    cache = cache if cache is not None else {}
    budget = [_VERIFY_TOTAL_MAX_BYTES]
    def populate_page_cache() -> None:
        if source_page_url in cache:
            return
        page_html = _fetch(source_page_url, "text/html", _PAGE_MAX_BYTES, budget, audit, url_policy)
        if cache_html is not None:
            cache_html[source_page_url] = (
                page_html.decode("utf-8", "ignore") if page_html is not None else None
            )
        cache[source_page_url] = (
            _image_urls_in_page(page_html.decode("utf-8", "ignore"), source_page_url)
            if page_html is not None
            else None
        )

    # Varios itens do plano podem apontar para a mesma galeria. O lock protege
    # o "ausente -> baixar -> gravar": evita requests duplicadas, sem pular a
    # comparacao byte-a-byte da imagem individual logo abaixo.
    if cache_lock is None:
        populate_page_cache()
    else:
        with cache_lock:
            populate_page_cache()
    listed = cache[source_page_url]
    if listed is None:
        return False, _coded_reason(
            "source_page_unavailable",
            "pagina de origem inacessivel; nao e possivel confirmar a origem da imagem",
        )
    if not listed:
        return False, _coded_reason(
            "source_scan_incomplete",
            "pagina acessivel, mas nao foram encontradas referencias de imagem",
        )
    try:
        downloaded_bytes = downloaded.read_bytes()
        downloaded_hash = _md5(downloaded_bytes)
    except OSError as exc:
        return False, _coded_reason("candidate_asset_unavailable", f"falha ao ler o arquivo baixado: {exc}")
    if not downloaded_bytes:
        return False, _coded_reason(
            "candidate_asset_unavailable", "arquivo baixado vazio; nao ha bytes para comparar"
        )

    direct_normalized = _normalized_url(direct_image_url)
    exact = [url for url in listed if _normalized_url(url) == direct_normalized]
    exact_downloaded = False
    exact_compared = False
    exact_decode_failed = False
    for url in exact:
        data = _fetch(url, "image/*", _IMG_MAX_BYTES, budget, audit, url_policy)
        if data is not None:
            exact_downloaded = True
            if _md5(data) == downloaded_hash:
                return True, _coded_reason(
                    "source_reference_found", "URL exata confirmada na pagina de origem (bytes iguais)"
                )
            same_frame, level = verify_image_identity(downloaded_bytes, data)
            if same_frame:
                return True, _coded_reason(
                    "source_reference_found", f"URL exata confirmada na pagina de origem ({level.lower()})"
                )
            exact_compared = exact_compared or level == "MISMATCH"
            exact_decode_failed = exact_decode_failed or level == "UNVERIFIED"
    if exact:
        if exact_compared:
            return False, _coded_reason(
                "source_bytes_mismatch",
                "CDN serviu conteudo divergente da URL exata listada na pagina de origem "
                "(bytes diferentes); fonte instavel — troque a URL da imagem",
            )
        if exact_decode_failed:
            return False, _coded_reason(
                "source_decode_failed",
                "a referência exata não pôde ser decodificada como imagem",
            )
        return False, _coded_reason(
            "source_asset_unavailable",
            "nao foi possivel baixar a URL exata para comparar os bytes",
        )

    slug = _slug(direct_image_url)
    same_slug = [url for url in listed if _slug(url) == slug]
    rendition_key = _asset_rendition_key(direct_image_url)
    same_rendition = [
        url for url in listed
        if rendition_key and _asset_rendition_key(url) == rendition_key and url not in same_slug
    ]
    comparison_candidates = list(dict.fromkeys([*same_slug, *same_rendition]))
    downloads = 0
    compared = 0
    unavailable = 0
    decode_failed = False
    for url in comparison_candidates:
        if downloads >= _MAX_DOWNLOADS:
            break
        downloads += 1
        data = _fetch(url, "image/*", _IMG_MAX_BYTES, budget, audit, url_policy)
        if data is None:
            unavailable += 1
            continue
        compared += 1
        if _md5(data) == downloaded_hash:
            via = "slug" if url in same_slug else "rendicao CDN"
            return True, _coded_reason(
                "source_reference_found", f"imagem confirmada na pagina de origem por {via} e bytes"
            )
        same_frame, level = verify_image_identity(downloaded_bytes, data)
        if same_frame:
            via = "slug" if url in same_slug else "rendicao CDN"
            return True, _coded_reason(
                "source_reference_found", f"imagem confirmada na pagina de origem por {via} ({level.lower()})"
            )
        decode_failed = decode_failed or level == "UNVERIFIED"
    if comparison_candidates:
        if compared:
            if unavailable or downloads < len(comparison_candidates):
                return False, _coded_reason(
                    "source_scan_incomplete",
                    "uma ou mais variantes da referência não puderam ser examinadas; "
                    "a divergência parcial não prova mismatch",
                )
            if decode_failed:
                return False, _coded_reason(
                    "source_decode_failed",
                    "uma referência da mesma imagem não pôde ser decodificada",
                )
            return False, _coded_reason(
                "source_bytes_mismatch",
                "CDN serviu conteudo divergente da pagina de origem para a mesma imagem "
                "(bytes diferentes); fonte instavel — troque a URL da imagem",
            )
        if decode_failed:
            if downloads < len(comparison_candidates):
                return False, _coded_reason(
                    "source_scan_incomplete",
                    "a varredura das variantes da referência não foi concluída",
                )
            return False, _coded_reason(
                "source_decode_failed", "referência da mesma imagem não pôde ser decodificada"
            )
        if downloads < len(comparison_candidates):
            return False, _coded_reason(
                "source_scan_incomplete",
                "a varredura das variantes da referência não foi concluída",
            )
        return False, _coded_reason(
            "source_asset_unavailable", "referência da mesma imagem indisponível para comparação"
        )
    for url in listed:
        if downloads >= _MAX_DOWNLOADS:
            break
        downloads += 1
        data = _fetch(url, "image/*", _IMG_MAX_BYTES, budget, audit, url_policy)
        if data is not None:
            compared += 1
            if _md5(data) == downloaded_hash:
                return True, _coded_reason(
                    "source_reference_found", "imagem confirmada na pagina de origem (bytes iguais)"
                )
            same_frame, level = verify_image_identity(downloaded_bytes, data)
            if same_frame:
                return True, _coded_reason(
                    "source_reference_found", f"imagem confirmada na pagina de origem ({level.lower()})"
                )
            decode_failed = decode_failed or level == "UNVERIFIED"
        else:
            unavailable += 1
    if downloads < len(listed) and (downloads >= _MAX_DOWNLOADS or budget[0] <= 0):
        return False, _coded_reason(
            "source_scan_incomplete",
            "limite de comparações ou bytes atingido antes de examinar toda a origem",
        )
    if decode_failed and not compared:
        return False, _coded_reason(
            "source_decode_failed", "referências da página não puderam ser decodificadas"
        )
    if decode_failed:
        return False, _coded_reason(
            "source_decode_failed",
            "uma ou mais referências da página não puderam ser decodificadas",
        )
    if unavailable:
        return False, _coded_reason(
            "source_scan_incomplete",
            "uma ou mais referências da página ficaram indisponíveis; "
            "a divergência parcial não prova mismatch",
        )
    return False, _coded_reason(
        "source_mismatch",
        "imagem baixada nao consta na pagina de origem (slug ausente e bytes nao "
        "correspondem a nenhuma imagem listada); troque por uma imagem da pagina",
    )


def verify_downloaded_against_source_result(**kwargs: Any) -> dict[str, Any]:
    """Return structured provenance verification while retaining the tuple API."""
    return verification_result(*verify_downloaded_against_source(**kwargs))
