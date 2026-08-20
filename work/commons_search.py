"""Commons candidate discovery for editorial media plans (read-only)."""
import json, re, sys, urllib.parse, urllib.request

def clean_url(url):
    if not url:
        return url
    return re.sub(r"[?&]utm_[^&]+", "", url).rstrip("?&")

API = "https://commons.wikimedia.org/w/api.php"
HEADERS = {"User-Agent": "unicorniohater-editor/0.1 (editorial dry-run; contact: ops@unicorniohater.com.br)"}

def api(params):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def search(query, limit=12):
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": query, "gsrnamespace": "6", "gsrlimit": str(limit),
        "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime",
        "iiurlwidth": "1280",
    }
    try:
        data = api(params)
    except Exception as exc:
        return {"error": str(exc)}
    pages = (data.get("query") or {}).get("pages") or {}
    out = []
    for page in pages.values():
        ii = (page.get("imageinfo") or [{}])[0]
        ext = ii.get("extmetadata") or {}
        def meta(key):
            v = ext.get(key) or {}
            return re.sub(r"<[^>]+>", "", v.get("value", "")).strip()
        license_name = meta("LicenseShortName")
        if not license_name or license_name.lower() in {"fair use", "non-free"}:
            continue
        page_url = "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(page.get("title", "").replace(" ", "_"), safe="/:")
        out.append({
            "title": page.get("title"),
            "file_page": page.get("canonicalurl") or page_url,
            "direct_url": clean_url(ii.get("url")),
            "thumb_url": clean_url(ii.get("thumburl")),
            "width": ii.get("width"), "height": ii.get("height"), "mime": ii.get("mime"),
            "license": license_name,
            "license_url": meta("LicenseUrl"),
            "artist": meta("Artist"),
            "credit": meta("Credit"),
            "description": meta("ImageDescription")[:220],
            "date": meta("DateTimeOriginal") or meta("DateTime"),
        })
    return out

if __name__ == "__main__":
    queries = json.loads(sys.argv[1])
    for q in queries:
        print("=" * 100)
        print("QUERY:", q)
        for c in search(q):
            print(json.dumps(c, ensure_ascii=False))
