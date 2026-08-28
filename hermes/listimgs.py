import subprocess, re

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

pages = {
    "trio_newsx": "https://www.newsx.com/bl-news/trio-the-valley-of-doom-brings-an-original-indian-anime-style-fantasy-universe-to-cinemas-on-november-20-2026-265493/",
    "trio_punjabkesari": "https://english.punjabkesari.com/business/trio-the-valley-of-doom-brings-an-original-indian-anime-style-fantasy-universe-to-cinemas-on-november-20-2026/",
    "fe_mynintendonews": "https://mynintendonews.com/2026/08/28/fire-emblem-three-houses-added-to-nintendo-music/",
    "fe_nintendolife": "https://www.nintendolife.com/news/2026/08/fire-emblem-three-houses-returns-in-nintendo-musics-latest-album-update",
    "fe_nowplaying": "https://nowplaying.cool/nintendo-music-fire-emblem-three-houses-ost-drop/",
}

for name, url in pages.items():
    try:
        r = subprocess.run(["curl", "-sL", "-A", UA, "--max-time", "40", url],
                           capture_output=True, text=True, timeout=60)
        html = r.stdout
        if not html:
            print(f"### {name}: EMPTY (rc={r.returncode})"); continue
        srcs = []
        seen = set()
        for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.I):
            s = m.group(1)
            if s not in seen:
                seen.add(s); srcs.append(s)
        og = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, flags=re.I) \
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, flags=re.I)
        print(f"### {name} ({url})")
        print(f"  total_unique_imgs={len(srcs)} og:image={og.group(1) if og else 'N/A'}")
        for i, s in enumerate(srcs[:25]):
            print(f"  {i:2d} {s[:150]}")
    except Exception as e:
        print(f"### {name}: ERROR {e}")
