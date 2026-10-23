from __future__ import annotations

import importlib.util
import ssl
import sys
import types

import pytest

from unicornio_editor.media import search
from unicornio_editor.media.duckduckgo_images import search_duckduckgo_images


class _DuckduckgoImages:
    headers_update = {
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Referer": "https://duckduckgo.com/",
    }
    response_status = 200
    response_statuses = {}

    def __init__(self, *, timeout, verify):
        self.timeout = timeout
        self.verify = verify
        self.call = None
        self.last_http_status = 200
        self.result_shape_verified = True
        self.headers_seen = None
        self.http_client = types.SimpleNamespace(
            request=self._request
        )

    def _request(self, _method, url, **_kwargs):
        status = self.response_statuses.get(url, self.response_status)
        return types.SimpleNamespace(status_code=status, text="", content=b"")

    def search(self, query, **kwargs):
        self.call = (query, kwargs)
        self.headers_seen = dict(self.headers_update)
        self.http_client.request("GET", "https://duckduckgo.com/")
        self.http_client.request("GET", "https://duckduckgo.com/i.js")
        return [
            types.SimpleNamespace(
                image="https://cdn.example/game.jpg",
                url="https://publisher.example/game",
                title="Game cover",
                thumbnail="https://thumb.example/game.jpg",
                source="Bing",
            ),
            types.SimpleNamespace(
                image="https://cdn.example/no-source.jpg",
                url="",
                title="Missing source",
                thumbnail="",
                source="Bing",
            ),
        ]


def test_optional_ddgs_dependency_exposes_pinned_image_engine():
    if importlib.util.find_spec("ddgs") is None:
        pytest.skip("the optional media-search extra is not installed")

    from ddgs.engines.duckduckgo_images import DuckduckgoImages

    assert callable(getattr(DuckduckgoImages, "search", None))
    assert DuckduckgoImages.provider == "bing"


def test_ddgs_adapter_removes_http2_hop_header_and_requires_source_page(monkeypatch, tmp_path):
    engines = []

    class FakeEngine(_DuckduckgoImages):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            engines.append(self)

    monkeypatch.setitem(
        sys.modules,
        "ddgs.engines.duckduckgo_images",
        types.SimpleNamespace(DuckduckgoImages=FakeEngine),
    )
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text("test CA bundle", encoding="utf-8")
    monkeypatch.setenv("EDITOR_DDGS_CA_BUNDLE", str(ca_bundle))
    report = {}
    candidates = search_duckduckgo_images(
        "A Game", ratio="w", limit=5, timeout=3.2, report=report
    )

    assert len(candidates) == 1
    assert candidates[0]["engine"] == "duckduckgo"
    assert candidates[0]["discovery_method"] == "ddgs_images"
    assert candidates[0]["discovery_source"] == "Bing"
    assert candidates[0]["usable"] is True
    assert engines[0].timeout == 4
    assert engines[0].verify == str(ca_bundle)
    assert engines[0].call == (
        "A Game",
        {
            "region": "br-pt",
            "safesearch": "moderate",
            "layout": "Wide",
            "page": 1,
        },
    )
    assert "Connection" not in engines[0].headers_seen
    assert report["objects_parsed"] == 2
    assert report["candidates"] == 1
    assert report["usable_candidates"] == 1
    assert report["missing_source_candidates"] == 1
    assert report["discovery_source_counts"] == {"Bing": 1}
    assert report["failure_kind"] == "ok"
    assert report["upstream_provider"] == "bing"
    assert report["failure_stage"] == ""
    assert report["results_verified"] is True
    assert report["outcome"] == "results_found"
    assert report["request_observations"] == [
        {"stage": "vqd_bootstrap", "http_status": 200},
        {"stage": "image_search", "http_status": 200},
    ]


def test_ddgs_missing_dependency_is_reported_and_not_exhaustion(monkeypatch):
    monkeypatch.setitem(sys.modules, "ddgs.engines.duckduckgo_images", None)
    report = {}

    assert search_duckduckgo_images("query", report=report) == []
    assert report["failure_kind"] == "provider_unavailable"
    assert "not installed" in report["error"]


def test_ddgs_exception_is_technical_not_legitimate_empty(monkeypatch):
    class DDGSException(Exception):
        pass

    class RaisingClient(_DuckduckgoImages):
        def search(self, _query, **_kwargs):
            raise DDGSException("https://signed.example/?token=secret")

    monkeypatch.setitem(
        sys.modules,
        "ddgs.engines.duckduckgo_images",
        types.SimpleNamespace(DuckduckgoImages=RaisingClient),
    )
    report = {}

    assert search_duckduckgo_images("query", report=report) == []
    assert report["failure_kind"] == "provider_error"
    assert report["error"] == "DDGSException"
    assert "secret" not in str(report)


def test_non_200_ddgs_response_is_technical_not_verified_empty(monkeypatch):
    class ChallengeResponse(_DuckduckgoImages):
        response_statuses = {"https://duckduckgo.com/": 202}

    monkeypatch.setitem(
        sys.modules,
        "ddgs.engines.duckduckgo_images",
        types.SimpleNamespace(DuckduckgoImages=ChallengeResponse),
    )
    report = {}

    assert search_duckduckgo_images("query", report=report) == []
    assert report["http_status"] == 202
    assert report["failure_kind"] == "provider_error"
    assert report["error"] == "http_status_202"
    assert report["failure_stage"] == "vqd_bootstrap"
    assert report["outcome"] == "provider_unavailable"
    assert report["results_verified"] is False
    assert report["request_observations"] == [
        {"stage": "vqd_bootstrap", "http_status": 202},
    ]


def test_ddgs_image_endpoint_202_is_attributed_without_url_or_body(monkeypatch):
    class ImageChallenge(_DuckduckgoImages):
        response_statuses = {"https://duckduckgo.com/i.js": 202}

    monkeypatch.setitem(
        sys.modules,
        "ddgs.engines.duckduckgo_images",
        types.SimpleNamespace(DuckduckgoImages=ImageChallenge),
    )
    report = {}

    assert search_duckduckgo_images("private query", report=report) == []
    assert report["failure_stage"] == "image_search"
    assert report["http_status"] == 202
    assert report["failure_kind"] == "provider_error"
    assert report["results_verified"] is False
    assert report["request_observations"] == [
        {"stage": "vqd_bootstrap", "http_status": 200},
        {"stage": "image_search", "http_status": 202},
    ]
    assert "private query" not in str(report)
    assert "duckduckgo.com" not in str(report)


def test_valid_empty_ddgs_result_shape_is_distinct_from_transport_failure(monkeypatch):
    class EmptyResult(_DuckduckgoImages):
        def search(self, _query, **_kwargs):
            self.http_client.request("GET", "https://duckduckgo.com/")
            return []

    monkeypatch.setitem(
        sys.modules,
        "ddgs.engines.duckduckgo_images",
        types.SimpleNamespace(DuckduckgoImages=EmptyResult),
    )
    report = {}

    assert search_duckduckgo_images("query", report=report) == []
    assert report["http_status"] == 200
    assert report["failure_kind"] == "no_results_legitimate"
    assert report["results_verified"] is True
    assert report["outcome"] == "verified_no_results"


def test_unreadable_explicit_ca_bundle_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setitem(
        sys.modules,
        "ddgs.engines.duckduckgo_images",
        types.SimpleNamespace(DuckduckgoImages=_DuckduckgoImages),
    )
    monkeypatch.setenv("EDITOR_DDGS_CA_BUNDLE", str(tmp_path / "missing.pem"))
    report = {}

    assert search_duckduckgo_images("query", report=report) == []
    assert report["failure_kind"] == "provider_error"
    assert report["error"] == "ca_bundle_unavailable"


def test_optional_ddgs_failure_falls_through_to_bing(monkeypatch):
    calls = []

    def ddg_failure(_query, report=None, **_kwargs):
        calls.append("duckduckgo")
        report.update({"failure_kind": "provider_error", "error": "DDGSException"})
        return []

    def bing_candidate(query, report=None, **_kwargs):
        calls.append("bing")
        report.update({"failure_kind": "ok", "candidates": 1})
        return [{
            "query": query,
            "direct_image_url": "https://cdn.example/approved.jpg",
            "source_page_url": "https://publisher.example/game",
            "usable": True,
            "engine": "bing",
        }]

    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_PROVIDERS", "duckduckgo,bing,yandex")
    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_REQUIRED_PROVIDERS", "bing,yandex")
    monkeypatch.setattr(
        "unicornio_editor.media.duckduckgo_images.search_duckduckgo_images",
        ddg_failure,
    )
    monkeypatch.setattr(search, "search_bing_images", bing_candidate)
    monkeypatch.setattr(
        search, "search_yandex_images",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Yandex should not run")),
    )
    result = search.search_web_images_batch(
        ["query"],
        limit=1,
        accept=lambda candidates, _query: sum(bool(item.get("usable")) for item in candidates),
    )[0]

    assert calls == ["duckduckgo", "bing"]
    assert result["completion_reason"] == "TARGET_REACHED"
    assert result["engine_summary"]["optional_provider_failures"] == 1
    assert result["engine_summary"]["optional_failure_engines"] == ["duckduckgo"]
    assert result["engine_summary"]["not_attempted_engines"] == ["yandex"]


def test_ddg_http_202_isolated_and_bing_fallback_can_reach_capacity(monkeypatch, tmp_path):
    calls = []

    class BootstrapChallenge(_DuckduckgoImages):
        response_statuses = {"https://duckduckgo.com/": 202}

    monkeypatch.setitem(
        sys.modules,
        "ddgs.engines.duckduckgo_images",
        types.SimpleNamespace(DuckduckgoImages=BootstrapChallenge),
    )
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text("test CA bundle", encoding="utf-8")
    monkeypatch.setenv("EDITOR_DDGS_CA_BUNDLE", str(ca_bundle))
    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_PROVIDERS", "duckduckgo,bing,yandex")
    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_REQUIRED_PROVIDERS", "bing,yandex")

    def bing_candidate(query, report=None, **_kwargs):
        calls.append("bing")
        report.update({"failure_kind": "ok", "candidates": 1})
        return [{
            "query": query,
            "direct_image_url": "https://cdn.example/approved.jpg",
            "source_page_url": "https://publisher.example/game",
            "usable": True,
            "engine": "bing",
        }]

    monkeypatch.setattr(search, "search_bing_images", bing_candidate)
    monkeypatch.setattr(
        search,
        "search_yandex_images",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("capacity should stop before Yandex")
        ),
    )
    result = search.search_web_images_batch(
        ["query"],
        limit=1,
        accept=lambda candidates, _query: sum(
            bool(item.get("usable")) for item in candidates
        ),
    )[0]

    assert result["engine_reports"]["duckduckgo"]["failure_kind"] == "provider_error"
    assert result["engine_reports"]["duckduckgo"]["http_status"] == 202
    assert result["engine_reports"]["duckduckgo"]["failure_stage"] == "vqd_bootstrap"
    assert result["completion_reason"] == "TARGET_REACHED"
    assert result["candidates"][0]["engine"] == "bing"
    assert calls == ["bing"]


def test_ddg_tls_failure_is_isolated_and_bing_fallback_can_reach_capacity(monkeypatch, tmp_path):
    class TLSFailure(_DuckduckgoImages):
        def _request(self, _method, _url, **_kwargs):
            raise ssl.SSLError("certificate verification failed")

    monkeypatch.setitem(
        sys.modules,
        "ddgs.engines.duckduckgo_images",
        types.SimpleNamespace(DuckduckgoImages=TLSFailure),
    )
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text("test CA bundle", encoding="utf-8")
    monkeypatch.setenv("EDITOR_DDGS_CA_BUNDLE", str(ca_bundle))
    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_PROVIDERS", "duckduckgo,bing,yandex")
    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_REQUIRED_PROVIDERS", "bing,yandex")

    def bing_candidate(query, report=None, **_kwargs):
        report.update({"failure_kind": "ok", "candidates": 1})
        return [{
            "query": query,
            "direct_image_url": "https://cdn.example/approved.jpg",
            "source_page_url": "https://publisher.example/game",
            "usable": True,
            "engine": "bing",
        }]

    monkeypatch.setattr(search, "search_bing_images", bing_candidate)
    monkeypatch.setattr(
        search,
        "search_yandex_images",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("capacity should stop before Yandex")
        ),
    )
    result = search.search_web_images_batch(
        ["query"],
        limit=1,
        accept=lambda candidates, _query: sum(
            bool(item.get("usable")) for item in candidates
        ),
    )[0]

    assert result["engine_reports"]["duckduckgo"]["failure_kind"] == "provider_error"
    assert result["engine_reports"]["duckduckgo"]["error"] == "SSLError"
    assert result["completion_reason"] == "TARGET_REACHED"
    assert result["candidates"][0]["engine"] == "bing"
