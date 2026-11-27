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
import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from pathlib import Path
from html import escape, unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
from .media.text import plain_text, split_game_names

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def trailer_search_query(game_name: str) -> str:
    """Build a plain-text deterministic trailer query from the game identity."""
    return "; ".join(f"{name} trailer" for name in split_game_names(game_name))


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
_TRAILER_CACHE_FILENAME = "trailer_cache.json"
_TRAILER_FOUND_TTL = timedelta(days=30)
_TRAILER_ABSENT_TTL = timedelta(hours=6)
# Legacy negatives could be written for incomplete search/metadata. They
# must not authorize waivers, even if their old timestamp is still fresh.
_TRAILER_ABSENCE_EVIDENCE_VERSION = 2


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
    trailer, _status = find_game_trailer_with_status(
        game_name, timeout=timeout, max_candidates=max_candidates
    )
    return trailer


def find_game_trailer_with_status(
    game_name: str,
    *,
    timeout: float = 15.0,
    max_candidates: int = 5,
) -> tuple[dict[str, str] | None, str]:
    """Discover a trailer and distinguish absence from a failed search."""
    if not isinstance(game_name, str) or not game_name.strip():
        raise TrailerError("game_name is required")
    name = " ".join(unescape(game_name).split())
    if max_candidates < 1:
        return None, "search_failed"
    candidates = _search_youtube(trailer_search_query(name), limit=max_candidates, timeout=timeout)
    if candidates is None:
        return None, "search_failed"
    unverified_candidates = False
    for candidate in candidates:
        title = candidate.get("title") or ""
        if not title:
            # A raw video ID is not a listing we can rule out, and oEmbed
            # alone cannot supply the independent channel consistency check.
            unverified_candidates = True
            continue
        if not _matches_game(title, name):
            continue
        if not _looks_like_trailer(title):
            if "trailer" in title.casefold():
                unverified_candidates = True
            continue
        video_id = candidate.get("video_id") or ""
        channel = candidate.get("channel") or ""
        if not channel:
            unverified_candidates = True
            continue
        meta = _fetch_oembed(video_id, timeout=timeout)
        meta_title = (meta or {}).get("title") or ""
        if (
            not meta
            or not _looks_like_trailer(meta_title)
            or not _matches_game(meta_title, name)
            or not _official_channel_consistent(title, channel, meta)
        ):
            unverified_candidates = True
            continue
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
        }, "found"
    return None, "unverified_candidates" if unverified_candidates else "official_not_found"


def find_cached_game_trailer_with_status(
    game_name: str, *, root: Path | None, timeout: float = 15.0, max_candidates: int = 5,
    discover=None,
) -> tuple[dict[str, str] | None, str]:
    """Reuse only verified trailer results; transport failures are never cached."""
    discover = discover or find_game_trailer_with_status
    if root is None:
        return discover(game_name, timeout=timeout, max_candidates=max_candidates)
    key = _cache_key(game_name)
    cache = _read_trailer_cache(root)
    entry = cache.get(key)
    if (
        isinstance(entry, dict)
        and _cache_entry_matches_game(entry, game_name)
        and _cache_fresh(entry)
    ):
        if entry.get("status") == "found" and isinstance(entry.get("trailer"), dict):
            return {str(k): str(v) for k, v in entry["trailer"].items()}, "found"
        if _negative_cache_checked_at(entry, game_name) is not None:
            return None, "official_not_found"
    trailer, status = discover(game_name, timeout=timeout, max_candidates=max_candidates)
    if status in {"found", "official_not_found"}:
        cache[key] = {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": status,
            "provider": "youtube",
            "query": trailer_search_query(game_name),
            "trailer": trailer,
            **({"absence_evidence_version": _TRAILER_ABSENCE_EVIDENCE_VERSION}
               if status == "official_not_found" else {}),
        }
        _write_trailer_cache(root, cache)
    return trailer, status


def _cache_key(game_name: str) -> str:
    tokens = re.findall(r"[^\W_]+", plain_text(game_name).casefold(), flags=re.UNICODE)
    canonical = " ".join(tokens)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest() if canonical else "unknown"


def _cache_entry_matches_game(entry: Any, game_name: str) -> bool:
    return (
        isinstance(entry, dict)
        and str(entry.get("provider") or "").strip().casefold() == "youtube"
        and str(entry.get("query") or "").strip().casefold()
        == trailer_search_query(game_name).casefold()
    )


def _trailer_cache_path(root: Path) -> Path:
    return root / "work" / _TRAILER_CACHE_FILENAME


def _read_trailer_cache(root: Path) -> dict[str, object]:
    try:
        data = json.loads(_trailer_cache_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_trailer_cache(root: Path, cache: dict[str, object]) -> None:
    try:
        path = _trailer_cache_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _parse_cache_timestamp(entry: dict[str, Any]) -> datetime | None:
    try:
        checked = datetime.fromisoformat(str(entry.get("checked_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return checked.astimezone(timezone.utc) if checked.tzinfo is not None else None


def _cache_fresh(entry: dict[str, Any]) -> bool:
    checked = _parse_cache_timestamp(entry)
    ttl = _TRAILER_FOUND_TTL if entry.get("status") == "found" else _TRAILER_ABSENT_TTL
    return checked is not None and timedelta(0) <= datetime.now(timezone.utc) - checked <= ttl


def _negative_cache_checked_at(entry: Any, game_name: str, *, now: datetime | None = None) -> datetime | None:
    if (
        not _cache_entry_matches_game(entry, game_name)
        or entry.get("status") != "official_not_found"
        or entry.get("absence_evidence_version") != _TRAILER_ABSENCE_EVIDENCE_VERSION
    ):
        return None
    checked = _parse_cache_timestamp(entry)
    current = now or datetime.now(timezone.utc)
    if checked is None or current.tzinfo is None:
        return None
    return checked if timedelta(0) <= current.astimezone(timezone.utc) - checked <= _TRAILER_ABSENT_TTL else None


def cached_game_trailer_checked_at(root: Path | None, game_name: str) -> str | None:
    """Return the oldest timestamp across the current negative search records."""
    if root is None or not isinstance(game_name, str) or not game_name.strip():
        return None
    cache = _read_trailer_cache(Path(root))
    checked_times: list[datetime] = []
    now = datetime.now(timezone.utc)
    for name in split_game_names(game_name):
        checked = _negative_cache_checked_at(cache.get(_cache_key(name)), name, now=now)
        if checked is None:
            return None
        checked_times.append(checked)
    return min(checked_times).isoformat(timespec="seconds") if checked_times else None


def trailer_search_evidence_is_recent(
    evidence: Mapping[str, Any] | None,
    game_name: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Validate a current, query-bound YouTube absence record for the READY gate."""
    if not isinstance(evidence, Mapping) or not isinstance(game_name, str) or not game_name.strip():
        return False
    if (
        str(evidence.get("provider") or "").strip().casefold() != "youtube"
        or evidence.get("result") != "official_not_found"
    ):
        return False
    expected_query = trailer_search_query(game_name).casefold()
    recorded_query = " ".join(unescape(str(evidence.get("query") or "")).split()).casefold()
    if not expected_query or recorded_query != expected_query:
        return False
    try:
        checked = datetime.fromisoformat(
            str(evidence.get("searched_at") or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False
    if checked.tzinfo is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return False
    age = current.astimezone(timezone.utc) - checked.astimezone(timezone.utc)
    return timedelta(0) <= age <= _TRAILER_ABSENT_TTL


def trailer_search_evidence_matches_cache(
    evidence: Mapping[str, Any] | None,
    game_name: str,
    root: Path | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Require a recent editorial waiver to match every persisted negative search."""
    if (
        root is None
        or not isinstance(evidence, Mapping)
        or not trailer_search_evidence_is_recent(evidence, game_name, now=now)
    ):
        return False
    names = split_game_names(game_name)
    if not names:
        return False
    cache = _read_trailer_cache(Path(root))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return False
    checked_times: list[datetime] = []
    for name in names:
        checked = _negative_cache_checked_at(cache.get(_cache_key(name)), name, now=current)
        if checked is None:
            return False
        checked_times.append(checked)
    try:
        evidence_checked = datetime.fromisoformat(
            str(evidence.get("searched_at") or "").replace("Z", "+00:00")
        )
    except (AttributeError, TypeError, ValueError):
        return False
    if evidence_checked.tzinfo is None:
        return False
    return evidence_checked.astimezone(timezone.utc) == min(checked_times)


def build_trailer_html(trailer: Mapping[str, Any]) -> str:
    """Build a safe, centralized embed with visible credit for the video."""
    if not isinstance(trailer, Mapping):
        raise TrailerError("trailer must be an object")
    video_id = str(trailer.get("video_id") or "")
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise TrailerError("invalid YouTube video id")
    # O embed e texto de exibicao: normaliza travessoes do titulo do video
    # para hifen (o gate de qualidade rejeita —/– no conteudo final).
    title_raw = re.sub(r"[\u2014\u2013]", "-", str(trailer.get("title") or "Trailer"))
    title = escape(title_raw)
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


def _search_youtube(query: str, *, limit: int, timeout: float) -> list[dict[str, str]] | None:
    url = f"{_YT_SEARCH_URL}?{urlencode({'search_query': query, 'hl': 'en', 'gl': 'US'})}"
    request = Request(url, headers=_BROWSER_HEADERS)
    try:
        with urlopen(request, timeout=timeout) as response:
            html = response.read(2_000_000).decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError):
        return None
    lowered = html.casefold()
    if any(marker in lowered for marker in (
        "captcha", "unusual traffic", "consent.youtube.com", "before you continue to youtube",
        "temporarily blocked", "enable javascript",
    )):
        return None
    videos = _parse_initial_data(html)
    if videos is None:
        # Recovered IDs are explicitly unverified, not an empty search.
        videos = _parse_video_ids_fallback(html)
        if not videos:
            return None
    return videos[:limit]


def _parse_initial_data(html: str) -> list[dict[str, str]] | None:
    """Return an understood result set (possibly empty), or an unknown page."""
    match = re.search(r"ytInitialData\s*=\s*(\{.*?\})\s*;\s*</script>", html, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        contents = data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"][
            "sectionListRenderer"
        ]["contents"]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(contents, list):
        return None
    if not contents:
        return []
    videos: list[dict[str, str]] = []
    saw_known_empty = False
    saw_unknown = False
    for section in contents:
        item_section = section.get("itemSectionRenderer") if isinstance(section, dict) else None
        if not isinstance(item_section, dict) or not isinstance(item_section.get("contents"), list):
            saw_unknown = True
            continue
        items = item_section["contents"]
        if not items:
            saw_known_empty = True
        for item in items:
            renderer = item.get("videoRenderer") if isinstance(item, dict) else None
            if not isinstance(renderer, dict):
                message = item.get("messageRenderer") if isinstance(item, dict) else None
                if (
                    isinstance(message, dict)
                    and _runs_text(message.get("text")).casefold().rstrip(".") in {
                        "no results found", "no results found for your search",
                    }
                ):
                    saw_known_empty = True
                else:
                    saw_unknown = True
                continue
            video_id = renderer.get("videoId")
            videos.append({
                "video_id": video_id if isinstance(video_id, str) else "",
                "title": _runs_text(renderer.get("title")),
                "channel": _runs_text(renderer.get("ownerText")),
            })
    if videos:
        return videos
    return [] if saw_known_empty and not saw_unknown else None


def _parse_video_ids_fallback(html: str) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for video_id in re.findall(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"', html):
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
    folded = title.casefold()
    disqualifiers = (
        "fan trailer", "fan-made", "fan made", "unofficial", "reaction",
        "gameplay", "walkthrough", "concept trailer", "trailer edit", "parody",
    )
    if any(marker in folded for marker in disqualifiers):
        return False
    return "trailer" in folded and "official" in folded


def _official_channel_consistent(title: str, candidate_channel: str, meta: dict[str, str]) -> bool:
    """Require an official-title signal and matching YouTube channel metadata.

    YouTube search/oEmbed cannot prove publisher ownership; this consistency
    check rejects obvious fan/misattributed candidates and never turns a search
    transport failure into an official-absence waiver.
    """
    if not _looks_like_trailer(title) or not candidate_channel.strip():
        return False
    author = str(meta.get("author_name") or "").strip()
    try:
        author_url = urlparse(str(meta.get("author_url") or ""))
        youtube_url = author_url.scheme == "https" and (author_url.hostname or "").casefold() in {
            "youtube.com", "www.youtube.com", "m.youtube.com",
        }
    except ValueError:
        return False
    if not youtube_url:
        return False
    candidate_tokens = _normalize(candidate_channel)
    author_tokens = _normalize(author)
    return bool(candidate_tokens and author_tokens and candidate_tokens == author_tokens)


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
