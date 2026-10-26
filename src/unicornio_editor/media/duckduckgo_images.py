"""DuckDuckGo Images adapter using the optional ``ddgs`` package.

The backend is unofficial and can be blocked or change without notice. The
adapter intentionally treats library exceptions as provider failures, never as
proof that a query had no results. Every returned item still needs a verifiable
source page and passes through the same downstream media gates as other
providers.
"""

from __future__ import annotations

import json
import os
import re
import ssl
from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class _DDGSHTTPStatusError(RuntimeError):
    """Stop a DDGS request chain at its first non-success HTTP response."""

    def __init__(self, stage: str, status: int) -> None:
        super().__init__(f"DDGS {stage} returned HTTP {status}")
        self.stage = stage
        self.status = status


def _request_stage(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Map a request to a fixed endpoint label without retaining its URL."""
    raw_url = args[1] if len(args) > 1 else kwargs.get("url", "")
    try:
        parsed = urlsplit(str(raw_url))
    except ValueError:
        return "unknown"
    if (parsed.hostname or "").casefold() != "duckduckgo.com":
        return "unknown"
    path = parsed.path.rstrip("/")
    if not path:
        return "vqd_bootstrap"
    if path == "/i.js":
        return "image_search"
    return "unknown"


def _failure_kind(exc: Exception) -> str:
    name = type(exc).__name__.casefold()
    if "rate" in name or "ratelimit" in name:
        return "rate_limited"
    if "timeout" in name or "connection" in name or "network" in name:
        return "network_error"
    return "provider_error"


_BOT_CHALLENGE_MARKERS = (
    b"captcha",
    b"challenge",
    b"unusual traffic",
    b"automated requests",
    b"bot detection",
    b"/sorry/",
)


def _safe_response_observation(stage: str, status: int, response: Any) -> dict[str, Any]:
    """Summarize a response without retaining headers beyond MIME or its body."""
    observation: dict[str, Any] = {"stage": stage, "http_status": status}
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        except Exception:  # noqa: BLE001 - third-party response objects vary
            content_type = ""
        if content_type and len(content_type) <= 80 and all(
            char.isalnum() or char in "/.+-" for char in content_type
        ):
            observation["content_type"] = content_type
    try:
        body = getattr(response, "content", b"")
    except Exception:  # noqa: BLE001 - telemetry must not break provider execution
        body = b""
    if isinstance(body, str):
        body = body.encode("utf-8", errors="replace")
    if isinstance(body, bytes):
        observation["body_bytes"] = len(body)
        inspected = body[:65536].lower()
        observation["challenge_marker_present"] = any(
            marker in inspected for marker in _BOT_CHALLENGE_MARKERS
        )
    return observation


def search_duckduckgo_images(
    query: str,
    *,
    size: str = "xga",
    ratio: str = "w",
    limit: int = 10,
    timeout: float = 15.0,
    report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Search only the DDG image backend and normalize safe candidate metadata."""
    output = report if report is not None else {}
    output.clear()
    output.update({
        "http_status": 0,
        "html_bytes": 0,
        "objects_parsed": 0,
        "candidates": 0,
        "usable_candidates": 0,
        "missing_source_candidates": 0,
        "failure_kind": "provider_error",
        "failure_stage": "",
        "outcome": "not_started",
        "results_verified": False,
        "request_observations": [],
        "parser_version": 1,
        "backend": "duckduckgo",
        # ddgs 9.16.0 labels its DuckDuckGo Images engine as Bing-backed.
        # Keep the endpoint's health separate, but don't imply independence.
        "upstream_provider": "bing",
    })
    _ = size  # DDGS uses named size classes; the editorial gate checks real bytes/dimensions.
    if timeout < 1:
        output.update({"failure_kind": "budget_exhausted", "outcome": "budget_exhausted"})
        return []
    try:
        from ddgs.engines.duckduckgo_images import DuckduckgoImages
    except ImportError:
        output.update({
            "failure_kind": "provider_unavailable",
            "error": "optional ddgs dependency is not installed",
            "outcome": "provider_unavailable",
        })
        return []

    try:
        verify = _ca_bundle_for_ddgs()
    except OSError:
        output.update({
            "failure_kind": "provider_error",
            "error": "ca_bundle_unavailable",
            "outcome": "provider_unavailable",
        })
        return []

    # ddgs 9.16.0 sends a Connection hop-by-hop header from its DuckDuckGo
    # Images engine. primp rejects it as malformed on HTTP/2 before making a
    # request. A local subclass avoids mutating the dependency's global engine
    # registry or header map while preserving TLS certificate verification.
    class _CompatibleDuckduckgoImages(DuckduckgoImages):
        headers_update = {
            key: value
            for key, value in DuckduckgoImages.headers_update.items()
            if key.casefold() != "connection"
        }

        last_http_status = 0
        last_request_stage = ""
        result_shape_verified = False

        def extract_results(self, html_text):
            payload = json.loads(html_text)
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise ValueError("invalid DuckDuckGo Images response shape")
            self.result_shape_verified = True
            return super().extract_results(html_text)

    layout = {"w": "Wide", "t": "Tall", "s": "Square"}.get(str(ratio or "").casefold())
    max_results = max(10, min(40, max(1, int(limit or 1)) * 3))
    engine = None
    try:
        engine = _CompatibleDuckduckgoImages(
            timeout=max(1, ceil(float(timeout))),
            verify=verify,
        )
        engine.request_observations = []
        original_http_request = engine.http_client.request

        def _track_http_status(*args, **kwargs):
            stage = _request_stage(args, kwargs)
            engine.last_request_stage = stage
            try:
                response = original_http_request(*args, **kwargs)
            except Exception:
                engine.request_observations.append({"stage": stage, "http_status": 0})
                raise
            engine.last_http_status = int(getattr(response, "status_code", 0) or 0)
            engine.request_observations.append(_safe_response_observation(
                stage, engine.last_http_status, response
            ))
            if engine.last_http_status != 200:
                # In particular, stop after a non-200 VQD bootstrap rather
                # than issuing i.js with a challenge/error response's content.
                raise _DDGSHTTPStatusError(stage, engine.last_http_status)
            return response

        # The VQD bootstrap request bypasses BaseSearchEngine.request; wrap the
        # per-instance client so both bootstrap and image requests are covered.
        engine.http_client.request = _track_http_status
        results = engine.search(
            str(query or "").strip(),
            region="br-pt",
            safesearch="moderate",
            page=1,
            layout=layout,
        )
    except Exception as exc:  # noqa: BLE001 - third-party failures stay provider-local
        status = int(getattr(engine, "last_http_status", 0) or 0)
        stage = str(getattr(engine, "last_request_stage", "") or "")
        output["http_status"] = status
        output["failure_stage"] = stage
        output["request_observations"] = list(
            getattr(engine, "request_observations", ())
        )[:2]
        output["outcome"] = "provider_unavailable"
        if status in (429, 503):
            failure_kind = "rate_limited"
        elif status >= 400:
            failure_kind = "http_error"
        elif status == 202 and stage == "vqd_bootstrap":
            # 202 is consistent with an anti-automation challenge, but status
            # alone cannot prove why DDG returned it; keep the name cautious.
            failure_kind = "bot_challenge_suspected"
        elif status and status != 200:
            failure_kind = "provider_error"
        else:
            failure_kind = _failure_kind(exc)
        output.update({
            "failure_kind": failure_kind,
            # Never persist exception text: some HTTP clients include full URLs.
            "error": f"http_status_{status}" if status and status != 200 else type(exc).__name__[:80],
        })
        return []

    http_status = int(getattr(engine, "last_http_status", 0) or 0)
    output["http_status"] = http_status
    output["failure_stage"] = ""
    output["request_observations"] = list(
        getattr(engine, "request_observations", ())
    )[:2]
    if http_status in (429, 503):
        output.update({"failure_kind": "rate_limited", "error": f"http_status_{http_status}"})
        return []
    if http_status >= 400:
        output.update({"failure_kind": "http_error", "error": f"http_status_{http_status}"})
        return []
    if http_status != 200:
        # A non-200 response (for example DDG's 202 challenge response) is not
        # evidence that the query legitimately had no images.
        output.update({"failure_kind": "provider_error", "error": f"http_status_{http_status or 'unknown'}"})
        return []

    if not isinstance(results, list):
        output.update({
            "failure_kind": "provider_error",
            "error": "invalid result shape",
            "failure_stage": str(getattr(engine, "last_request_stage", "") or ""),
            "outcome": "provider_unavailable",
        })
        return []

    output["results_verified"] = bool(
        getattr(engine, "result_shape_verified", False)
    )

    from .search import PARSER_VERSION, _candidate

    candidates: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    missing_source = 0
    for item in results:
        if item is None:
            continue
        image_url = getattr(item, "image", "")
        source_page = getattr(item, "url", "")
        if not image_url or not source_page:
            missing_source += 1
            continue
        candidate = _candidate(
            str(query or "").strip(),
            str(size or ""),
            str(image_url),
            str(source_page),
            str(getattr(item, "title", "") or ""),
            str(getattr(item, "thumbnail", "") or ""),
            engine="duckduckgo",
            discovery_method="ddgs_images",
        )
        source_label = re.sub(
            r"[^A-Za-z0-9 ._-]", "_", str(getattr(item, "source", "") or "unknown")
        ).strip()[:40] or "unknown"
        candidate["discovery_source"] = source_label
        source_counts[source_label] = source_counts.get(source_label, 0) + 1
        candidates.append(candidate)

    # DDGS' public aggregator ranks/deduplicates results after the backend
    # returns. Keep those useful properties without allowing a failed response
    # to be converted into a verified empty result.
    from ddgs.similarity import SimpleFilterRanker

    ranked = SimpleFilterRanker().rank(
        [
            {
                "href": str(candidate.get("source_page_url") or ""),
                "title": str(candidate.get("title") or ""),
                "body": str(candidate.get("discovery_source") or ""),
                "image": str(candidate.get("direct_image_url") or ""),
            }
            for candidate in candidates
        ],
        str(query or ""),
    )
    rank = {str(item.get("image") or ""): index for index, item in enumerate(ranked)}
    candidates.sort(key=lambda item: rank.get(str(item.get("direct_image_url") or ""), len(rank)))
    candidates = candidates[:max_results]

    usable = sum(bool(candidate.get("usable")) for candidate in candidates)
    unique_urls = {
        str(candidate.get("direct_image_url") or "")
        for candidate in candidates
        if candidate.get("direct_image_url")
    }
    failure_kind = "ok" if candidates else (
        "no_results_legitimate"
        if bool(getattr(engine, "result_shape_verified", False)) and not missing_source
        else "provider_error"
    )
    output.update({
        "objects_parsed": len(results),
        "candidates": len(candidates),
        "usable_candidates": usable,
        "candidates_with_page_reference": sum(
            bool(candidate.get("source_page_url")) for candidate in candidates
        ),
        "unique_image_urls": len(unique_urls),
        "missing_source_candidates": missing_source,
        "discovery_source_counts": dict(sorted(source_counts.items())),
        "parser_version": PARSER_VERSION,
        "failure_kind": failure_kind,
        "outcome": (
            "results_found" if candidates
            else "verified_no_results" if failure_kind == "no_results_legitimate"
            else "provider_unavailable"
        ),
    })
    if failure_kind != "no_results_legitimate" and not candidates:
        output["failure_stage"] = str(getattr(engine, "last_request_stage", "") or "")
    if not candidates:
        output["error"] = (
            "empty response without verifiable no-results evidence"
            if not getattr(engine, "result_shape_verified", False)
            else "no result contained both an image URL and a source page"
        )
    return candidates


def _ca_bundle_for_ddgs() -> bool | str:
    """Choose a readable PEM bundle without ever disabling TLS validation."""
    configured = os.environ.get("EDITOR_DDGS_CA_BUNDLE", "").strip()
    if configured:
        path = Path(configured).expanduser()
        with path.open("rb") as stream:
            if not stream.read(1):
                raise OSError("configured CA bundle is empty")
        return str(path)

    default_bundle = ssl.get_default_verify_paths().cafile
    if default_bundle:
        try:
            path = Path(default_bundle)
            with path.open("rb") as stream:
                if stream.read(1):
                    return str(path)
        except OSError:
            pass
    return True
