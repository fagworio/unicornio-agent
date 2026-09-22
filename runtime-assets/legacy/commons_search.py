#!/usr/bin/env python3
"""Busca arquivos no Wikimedia Commons (API) e imprime thumb 1280px utilizavel.

Uso (python do sistema basta, so stdlib):

    python3 commons_search.py "PlayStation 4 console" [width] [limit]
    python3 commons_search.py --titles "File:A.jpg" "File:B.jpg"

Saida: JSON por linha com title / thumb / page / size / license.
- `thumb` ja vem em thumb.wikimedia.org (o downloader do pipeline LE thumb.wikimedia.org;
  upload.wikimedia.org cru toma 429) e SEM querystring.
- A pagina do arquivo (`page`) lista essa URL -> `verify_downloaded_against_source` passa.

Pitfalls (run 2026-09-17):
- No media_plan, use license `public domain` (ingles) ou `Uso com credito`:
  `Domínio público` e RECUSADO pelo apply ("license is not accepted").
- Screenshot/arte plana (flat=True no precheck) serve INLINE, nunca como featured.
- Fotos de pessoas dificilmente passam a visao da featured (UNRELATED 0.90
  photograph): para posts de TV/programa prefira o frame de clipe do YouTube
  (img.youtube.com/vi/<id>/maxresdefault.jpg listado na pagina do artigo).
"""
import json
import sys
import urllib.parse
import urllib.request

UA = "UnicornioHaterEditor/1.0 (contato: redacao@unicorniohater.com.br)"
API = "https://commons.wikimedia.org/w/api.php"


def _query(params):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _rows(data):
    out = []
    for page in (data.get("query", {}).get("pages", {}) or {}).values():
        ii = (page.get("imageinfo") or [{}])[0]
        if ii.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
            continue
        if not ii.get("thumburl"):
            continue
        thumb = ii["thumburl"].split("?")[0].replace(
            "https://upload.wikimedia.org/", "https://thumb.wikimedia.org/"
        )
        em = ii.get("extmetadata", {})
        out.append({
            "title": page.get("title"),
            "thumb": thumb,
            "page": ii.get("descriptionurl"),
            "size": f"{ii.get('width')}x{ii.get('height')}",
            "license": (em.get("LicenseShortName", {}).get("value") or ""),
        })
    return out


def search(term, width=1280, limit=8):
    data = _query({
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": term, "gsrnamespace": "6", "gsrlimit": str(limit),
        "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime",
        "iiurlwidth": str(width),
    })
    return _rows(data)


def by_title(titles, width=1280):
    data = _query({
        "action": "query", "format": "json", "titles": "|".join(titles),
        "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime",
        "iiurlwidth": str(width),
    })
    return _rows(data)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--titles":
        rows = by_title(args[1:])
    else:
        term = args[0]
        width = int(args[1]) if len(args) > 1 else 1280
        limit = int(args[2]) if len(args) > 2 else 8
        rows = search(term, width, limit)
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
