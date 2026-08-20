"""Validation and deterministic discovery of official trailer embeds.

Two layers:
1. ``validate_trailer`` — strict validation for trailers proposed by the
   editorial model (requires an explicitly official source).
2. ``find_game_trailer`` / ``build_trailer_html`` — deterministic discovery:
   when the content is about a game, search YouTube for ``<game> trailer``,
   filter candidates by relevance, confirm existence/embeddability via the
   official oEmbed endpoint, and build a safe embed with visible credit.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from html import escape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class TrailerError(ValueError):
    """Raised when a trailer cannot be verified as official."""


_ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com"}
_YT_SEARCH_URL = "https://www.youtube.com/results"
_YT_OEMBED_URL = "https://www.youtube.com/oembed"
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
# Tokens too generic to be distinctive for a game-name match.
_STOPWORDS = {
    "the", "and", "for", "with", "from", "off", "into", "that", "this",
    "de", "do", "da", "dos", "das", "em", "no", "na", "para", "com", "por",
    "um", "uma", "uns", "umas", "e", "o", "os", "as", "a", "of", "vs", "x",
}


# ---------------------------------------------------------------------------
# Strict validation (editorial-model-provided trailers)
# ---------------------------------------------------------------------------

def validate_trailer(candidate: Mapping[str, Any] | None) -> dict[str, str] | None:
    if candidate is None:
        return None
    if not isinstance(candidate, Mapping) or set(candidate) != {"url", "channel_url", "official_source"}:
        raise TrailerError("trailer must contain url, channel_url and official_source")
    if candidate["official_source"] is not True:
        raise TrailerError("trailer source must be explicitly official")
    url = _url(candidate["url"], "url")
    channel_url = _url(candidate["channel_url"], "channel_url")
    url_host = (urlparse(url).hostname or "").lower()
    channel_host = (urlparse(channel_url).hostname or "").lower()
    if url_host not in _ALLOWED_HOSTS or channel_host not in _ALLOWED_HOSTS:
        raise TrailerError("trailer must use an allowlisted video platform")
    if url_host not in channel_host and channel_host not in url_host:
        raise TrailerError("trailer URL and official channel must use the same platform")
    return {"url": url, "channel_url": channel_url}


def _url(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrailerError(f"trailer {name} is required")
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise TrailerError(f"trailer {name} must be HTTPS")
    return value


# ---------------------------------------------------------------------------
# Deterministic discovery: search YouTube, validate via oEmbed
# ---------------------------------------------------------------------------

def find_game_trailer(
    game_name: str,
    *,
    timeout: float = 15.0,
    max_candidates: int = 5,
) -> dict[str, str] | None:
    """Search YouTube for ``<game_name> trailer`` and return the best match.

    Fail-closed: returns ``None`` when nothing relevant can be confirmed.
    """
    if not isinstance(game_name, str) or not game_name.strip():
        raise TrailerError("game_name is required")
    name = game_name.strip()
    candidates = _search_youtube(f"{name} trailer", limit=max_candidates, timeout=timeout)
    for candidate in candidates:
        title = candidate.get("title") or ""
        if not _looks_like_trailer(title):
            continue
        if not _matches_game(title, name):
            continue
        video_id = candidate.get("video_id") or ""
        meta = _fetch_oembed(video_id, timeout=timeout)
        if not meta:
            continue
        channel = candidate.get("channel") or ""
        return {
            "video_id": video_id,
            "title": meta.get("title") or title,
            "author_name": meta.get("author_name") or channel,
            "author_url": meta.get("author_url") or (
                f"https://www.youtube.com/@{quote(channel)}" if channel else ""
            ),
            "watch_url": f"https://www.youtube.com/watch?v={video_id}",
            "embed_url": f"https://www.youtube-nocookie.com/embed/{video_id}",
            "thumbnail_url": meta.get("thumbnail_url") or "",
            "matched_title": title,
        }
    return None


def build_trailer_html(trailer: Mapping[str, Any]) -> str:
    """Build a safe, centralized embed with visible credit for the video."""
    if not isinstance(trailer, Mapping):
        raise TrailerError("trailer must be an object")
    video_id = trailer.get("video_id")
    if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
        raise TrailerError("invalid YouTube video id")
    title = escape(str(trailer.get("title") or "Trailer"))
    watch_url = escape(str(trailer.get("watch_url") or f"https://www.youtube.com/watch?v={video_id}"), quote=True)
    author_name = escape(str(trailer.get("author_name") or "Canal oficial"))
    author_url = escape(str(trailer.get("author_url") or "https://www.youtube.com/"), quote=True)
    return (
        '<figure class="aligncenter">\n'
        f'<iframe width="560" height="315" src="https://www.youtube-nocookie.com/embed/{video_id}" '
        f'title="{title}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; '
        'encrypted-media; gyroscope; picture-in-picture; web-share" '
        'referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe>\n'
        f'<figcaption>Trailer: <a href="{watch_url}" target="_blank" rel="nofollow noopener">{title}</a>'
        f' · canal: <a href="{author_url}" target="_blank" rel="nofollow noopener">{author_name}</a>.</figcaption>\n'
        "</figure>"
    )


def _search_youtube(query: str, *, limit: int, timeout: float) -> list[dict[str, str]]:
    url = f"{_YT_SEARCH_URL}?{urlencode({'search_query': query, 'hl': 'en', 'gl': 'US'})}"
    request = Request(url, headers=_BROWSER_HEADERS)
    try:
        with urlopen(request, timeout=timeout) as response:
            html = response.read(2_000_000).decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError):
        return []
    videos = _parse_initial_data(html)
    if not videos:
        videos = _parse_video_ids_fallback(html)
    return videos[:limit]


def _parse_initial_data(html: str) -> list[dict[str, str]]:
    match = re.search(r"ytInitialData\s*=\s*(\{.+?\});\s*</script>", html, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    try:
        contents = data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"][
            "sectionListRenderer"
        ]["contents"]
    except (KeyError, TypeError):
        return []
    videos: list[dict[str, str]] = []
    for section in contents:
        items = section.get("itemSectionRenderer", {}).get("contents", [])
        for item in items:
            renderer = item.get("videoRenderer")
            if not renderer:
                continue
            video_id = renderer.get("videoId")
            title = _runs_text(renderer.get("title"))
            if not video_id or not title:
                continue
            videos.append(
                {
                    "video_id": str(video_id),
                    "title": title,
                    "channel": _runs_text(renderer.get("ownerText")),
                }
            )
    return videos


def _parse_video_ids_fallback(html: str) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for video_id in re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html):
        seen.setdefault(video_id, video_id)
    return [{"video_id": vid, "title": "", "channel": ""} for vid in seen]


def _runs_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        runs = value.get("runs")
        if isinstance(runs, list):
            return " ".join(str(run.get("text", "")) for run in runs if isinstance(run, dict)).strip()
        simple = value.get("simpleText")
        if isinstance(simple, str):
            return simple.strip()
    return ""


def _fetch_oembed(video_id: str, *, timeout: float) -> dict[str, str] | None:
    if not VIDEO_ID_RE.fullmatch(video_id or ""):
        return None
    url = f"{_YT_OEMBED_URL}?{urlencode({'format': 'json', 'url': f'https://www.youtube.com/watch?v={video_id}'})}"
    request = Request(url, headers=_BROWSER_HEADERS)
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read(1_000_000).decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "title": str(data.get("title") or "").strip(),
        "author_name": str(data.get("author_name") or "").strip(),
        "author_url": str(data.get("author_url") or "").strip(),
        "thumbnail_url": str(data.get("thumbnail_url") or "").strip(),
    }


def _looks_like_trailer(title: str) -> bool:
    return "trailer" in title.lower()


def _matches_game(title: str, game_name: str) -> bool:
    game_tokens = _normalize(game_name)
    title_tokens = _normalize(title)
    if not game_tokens:
        return False
    matched = game_tokens & title_tokens
    distinctive = {token for token in game_tokens if len(token) >= 6}
    return len(matched) / len(game_tokens) >= 0.5 or bool(distinctive & matched)


def _normalize(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", text)
    folded = folded.encode("ascii", "ignore").decode("ascii").lower()
    return {
        token
        for token in re.split(r"[^a-z0-9]+", folded)
        if token and token not in _STOPWORDS and len(token) >= 3
    }
