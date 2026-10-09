from types import SimpleNamespace
from unittest import mock

from unicornio_editor.pipeline_v2.model import FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress
from unicornio_editor.pipeline_v2.runtime import ProductionMediaResolver


class Config:
    dry_run = False
    http_timeout = 1
    vision_enabled = False
    remote_url_policy = "audit"


def test_media_resolver_normalizes_library_reuse(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    seen = {"plans": []}

    def fake_resolve(*_args, **_kwargs):
        return {"posts": [{"reuse": [{"url": "https://cdn.test/existing.webp", "source": "https://source.test/page", "media_id": 77, "author": "Test", "license": "CC BY", "license_url": "https://license.test", "captured_at": "2026-01-01T00:00:00Z", "credit_text": "Crédito", "alt_text": "Existing"}], "audit_candidates": []}]}

    def fake_validate(_client, editorial, **_kwargs):
        seen["plans"].append(editorial["media_plan"])
        return {"valid": True, "rejected": []}

    def fake_execute(editorial, *_args, **_kwargs):
        seen["featured_visual_asset"] = _kwargs.get("featured_visual_asset")
        return ([{"media_id": 77, "media_url": "https://cdn.test/existing.webp", "paragraph_index": 0, "alt_text": "existing", "credit_text": "Crédito da imagem: Existing", "featured": False}], None, None)

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "validate_media_plan", fake_validate)
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)
    previous = MediaProgress(required=2)
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "Test"}, SimpleNamespace(media=previous), {"cleaned_html": "<p>one</p>", "seo": {}}, previous
    )
    assert seen["plans"][0][0]["direct_image_url"] == "https://cdn.test/existing.webp"
    assert seen["plans"][0][0]["source_page_url"] == "https://source.test/page"
    assert seen["featured_visual_asset"] is None
    assert result.accepted == 1


def test_media_resolver_keeps_featured_and_inline_roles_separate(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    calls = []
    seen = {"plans": []}

    def candidate(url, subject, candidate_id):
        return {
            "candidate_id": candidate_id,
            "direct_image_url": url,
            "source_page_url": f"https://source.test/{candidate_id}",
            "subject": subject,
            "author": "Test",
            "license": "CC BY",
            "license_url": "https://license.test",
            "captured_at": "2026-01-01T00:00:00Z",
            "credit_text": "Crédito da imagem: Test",
            "alt_text": subject,
            "evidence": {"verdict": "deterministic_match", "score": 9},
        }

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        calls.append(batch["posts"][0]["query"])
        item = batch["posts"][0]
        role = "featured" if "-featured-" in batch["batch_id"] else "inline"
        audit = []
        if role == "featured":
            audit = [
                candidate("https://cdn.test/featured.webp", item["subject"], "featured"),
                candidate("https://cdn.test/featured-alt.webp", item["subject"], "featured-alt"),
            ]
        elif "Nana" in item["query"]:
            audit = [candidate("https://cdn.test/featured-alt.webp", item["subject"], "inline")]
        return {"posts": [{"audit_candidates": audit, "reuse": []}]}

    def fake_validate(_client, editorial, **_kwargs):
        seen["plans"].append(editorial["media_plan"])
        return {"valid": True, "rejected": []}

    def fake_execute(editorial, *_args, **_kwargs):
        return ([
            {
                "media_id": 101 if item["is_featured"] else 102,
                "media_url": item["direct_image_url"],
                "paragraph_index": item["paragraph_index"],
                "alt_text": item["alt_text"],
                "credit_text": item["credit_text"],
                "featured": item["is_featured"],
            }
            for item in editorial["media_plan"]
        ], None, None)

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "validate_media_plan", fake_validate)
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [
        {"subject": "Pluto"}, {"subject": "Nana"}
    ])

    previous = MediaProgress(required=2)
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "10 melhores animes"},
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Pluto e Nana</p>", "seo": {}},
        previous,
    )

    assert [[item["is_featured"] for item in plan] for plan in seen["plans"]] == [[True], [False]]
    assert seen["plans"][0][0]["direct_image_url"] == "https://cdn.test/featured.webp"
    assert seen["plans"][1][0]["direct_image_url"] == "https://cdn.test/featured-alt.webp"
    assert any("Nana anime" in query for query in calls)
    assert result.featured.status is runtime.FeaturedStatus.VALID


def test_media_resolver_refreshes_required_on_noop_retry(tmp_path):
    inline = tuple(
        InlineMedia(index, f"https://cdn.test/{index}.webp", index)
        for index in range(1, 5)
    )
    previous = MediaProgress(
        required=6,
        inline=inline,
        featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test content.</p>", "seo": {}},
        previous,
    )
    assert result.required == 2
    assert result.accepted == 4
    assert result.missing == 0


def test_media_resolver_passes_baseline_phashes_to_final_executor(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    captured = {}

    candidate = {
        "candidate_id": "new-frame",
        "direct_image_url": "https://cdn.test/new.webp",
        "source_page_url": "https://source.test/page",
        "subject": "Test",
        "author": "Test",
        "license": "CC BY",
        "license_url": "https://license.test",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito",
        "alt_text": "Test",
        "evidence": {"verdict": "deterministic_match", "score": 9},
    }

    def fake_resolve(*_args, **_kwargs):
        return {"posts": [{"reuse": [], "audit_candidates": [candidate]}]}

    def fake_validate(*_args, **_kwargs):
        return {"valid": True, "rejected": []}

    def fake_execute(_editorial, *_args, **kwargs):
        captured["previous_inline_phashes"] = kwargs["previous_inline_phashes"]
        return ([], None, None)

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(runtime, "validate_media_plan", fake_validate)
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)

    previous = MediaProgress(
        required=2,
        inline=(InlineMedia(77, "https://cdn.test/old.webp", 0, phash="baseline"),),
        featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test content.</p>", "seo": {}},
        previous,
    )

    assert captured["previous_inline_phashes"] == ("baseline",)
    assert result.accepted == 1


def test_media_resolver_blocks_final_duplicate_before_upload(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime
    import unicornio_editor.workflow as workflow

    candidate = {
        "candidate_id": "new-frame",
        "direct_image_url": "https://source.test/new.jpg",
        "source_page_url": "https://source.test/page",
        "subject": "Test",
        "author": "Test",
        "license": "CC BY",
        "license_url": "https://license.test",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito",
        "alt_text": "Test",
        "evidence": {"verdict": "deterministic_match", "score": 9},
    }

    monkeypatch.setattr(
        cli,
        "_resolve_media_batch",
        lambda *_args, **_kwargs: {"posts": [{"reuse": [], "audit_candidates": [candidate]}]},
    )
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(workflow, "_media_item_rejection", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(workflow, "download_image", lambda *_args, **_kwargs: tmp_path / "source.jpg")
    monkeypatch.setattr(workflow, "convert_to_webp", lambda *_args, **_kwargs: tmp_path / "inline.webp")
    monkeypatch.setattr(workflow, "verify_downloaded_against_source", lambda *_args, **_kwargs: (True, "verified"))
    monkeypatch.setattr(workflow, "image_dimensions", lambda *_args, **_kwargs: (1280, 720))
    monkeypatch.setattr(workflow, "image_has_transparency", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(workflow, "image_is_mostly_flat", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        "unicornio_editor.media.visual_hash.phash_from_path",
        lambda *_args, **_kwargs: "baseline",
    )
    upload = mock.Mock()
    monkeypatch.setattr(workflow, "upload_image", upload)

    previous = MediaProgress(
        required=2,
        inline=(InlineMedia(77, "https://cdn.test/old.webp", 0, phash="baseline"),),
        featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115088, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test content.</p>", "seo": {}},
        previous,
    )

    upload.assert_not_called()
    assert result.accepted == 1


def test_media_search_transient_engine_failure_is_not_completed(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    monkeypatch.setenv("EDITOR_GOOGLE_BROWSER_ENABLED", "true")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "_enriquecer_candidatos", lambda *_args, **_kwargs: ([], [], []))

    def fake_search(*_args, **_kwargs):
        return [{
            "query": "test query",
            "candidates": [],
            "engine_reports": {
                "google_browser": {"failure_kind": "ok"},
                "bing": {"failure_kind": "rate_limited"},
                "yandex": {"failure_kind": "ok"},
                "google": {"failure_kind": "ok"},
            },
        }]

    monkeypatch.setattr(search, "search_web_images_batch", fake_search)
    result = cli._resolve_media_batch(
        object(),
        Config(),
        tmp_path,
        {
            "schema_version": 1,
            "batch_id": "test-transient-search",
            "posts": [{
                "post_id": 115088,
                "subject": "Test",
                "query": "test query",
                "needed": 1,
                "limit": 3,
                "engine": "auto",
            }],
        },
        full=True,
        allow_reuse=False,
    )

    assert result["posts"][0]["search"]["completed"] is False


def test_media_search_healthy_zero_results_is_exhausted(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    monkeypatch.setenv("EDITOR_GOOGLE_BROWSER_ENABLED", "true")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "_enriquecer_candidatos", lambda *_args, **_kwargs: ([], [], []))

    def fake_search(*_args, **_kwargs):
        return [{
            "query": "empty query",
            "candidates": [],
            "engine_reports": {
                engine: {"failure_kind": "no_results_legitimate"}
                for engine in ("google_browser", "bing", "yandex", "google")
            },
        }]

    monkeypatch.setattr(search, "search_web_images_batch", fake_search)
    result = cli._resolve_media_batch(
        object(),
        Config(),
        tmp_path,
        {
            "schema_version": 1,
            "batch_id": "healthy-empty-search",
            "posts": [{
                "post_id": 115089,
                "subject": "Test",
                "query": "empty query",
                "needed": 1,
                "limit": 3,
                "engine": "auto",
            }],
        },
        full=True,
        allow_reuse=False,
    )

    progress = result["posts"][0]["search"]
    assert progress["completed"] is True
    # The batch resolver records discovery completion; the production resolver
    # adds final WebP/pHash exhaustion after executing the media plan.
    assert progress["exhausted"] is False
    assert progress["completion_reason"] == "EXHAUSTED"


def test_media_resolver_promotes_normalized_existing_featured(monkeypatch, tmp_path):
    import unicornio_editor.pipeline_v2.runtime as runtime
    import unicornio_editor.workflow as workflow

    calls = []

    def normalize(client, config, post, editorial, *, root):
        calls.append((post["featured_media"], root))
        return 88

    class Client:
        def get_media(self, media_id):
            return {"id": media_id, "source_url": "https://cdn.test/featured-1280x720.webp"}

    monkeypatch.setattr(workflow, "_normalize_existing_featured", normalize)
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 0)
    previous = MediaProgress(required=0, featured=FeaturedProgress(FeaturedStatus.MISSING))

    result = ProductionMediaResolver(Client(), Config(), tmp_path)(
        {
            "post_id": 115102,
            "title": "Test",
            "post": {"id": 115102, "featured_media": 7},
        },
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test.</p>", "seo": {}},
        previous,
    )

    assert calls == [(7, tmp_path)]
    assert result.featured.status is FeaturedStatus.VALID
    assert result.featured.media_id == 88
    assert result.featured.media_url.endswith("featured-1280x720.webp")


def test_media_resolver_continues_queries_after_final_rejections(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    def candidate(url, candidate_id):
        return {
            "candidate_id": candidate_id,
            "direct_image_url": url,
            "source_page_url": f"https://source.test/{candidate_id}",
            "subject": "Test",
            "author": "Test",
            "license": "CC BY",
            "license_url": "https://license.test",
            "captured_at": "2026-01-01T00:00:00Z",
            "credit_text": "Crédito",
            "alt_text": "Test",
            "evidence": {"verdict": "deterministic_match", "score": 9},
        }

    queries = []

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        query = batch["posts"][0]["query"]
        queries.append(query)
        candidates = (
            [candidate("https://cdn.test/rejected-a.webp", "a1"), candidate("https://cdn.test/rejected-b.webp", "a2")]
            if "Alpha" in query
            else [candidate("https://cdn.test/accepted.webp", "b1")]
        )
        return {"posts": [{
            "reuse": [],
            "audit_candidates": candidates,
            "search": {
                "completed": True,
                "queries_attempted": 1,
                "engines_attempted": ["bing"],
                "candidates_seen": len(candidates),
            },
        }]}

    def fake_execute(editorial, *_args, **_kwargs):
        item = editorial["media_plan"][0]
        if "rejected" in item["direct_image_url"]:
            return ([{"paragraph_index": item["paragraph_index"], "status": "rejected", "detail": "source_mismatch"}], None, None)
        return ([{
            "media_id": 88,
            "media_url": item["direct_image_url"],
            "paragraph_index": item["paragraph_index"],
            "alt_text": item["alt_text"],
            "credit_text": item["credit_text"],
            "featured": False,
            "phash": "accepted",
            "status": "accepted",
        }], None, None)

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [
        {"subject": "Alpha"}, {"subject": "Beta"}
    ])
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 3)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)

    baseline = InlineMedia(77, "https://cdn.test/baseline.webp", 0, phash="baseline")
    previous = MediaProgress(
        required=3,
        inline=(baseline,),
        featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115108, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test.</p>", "seo": {}},
        previous,
    )

    assert len(queries) == 2
    assert result.accepted == 2
    assert result.inline[0].media_id == 77
    assert result.search.queries_planned == 2
    assert result.search.queries_attempted == 2
    assert result.search.queries_completed == 2
    assert result.search.completed is True
    assert result.search.exhausted is True


def test_media_resolver_varies_normal_article_queries_before_retry(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    queries = []

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        query = batch["posts"][0]["query"]
        queries.append(query)
        return {"posts": [{
            "reuse": [], "audit_candidates": [],
            "search": {"completed": True, "queries_attempted": 1, "engines_attempted": ["yandex"]},
        }]}

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Star Fox"}])
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 2)

    previous = MediaProgress(
        required=2,
        featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"),
    )
    ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 114838, "title": "Star Fox game review"},
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test.</p>", "seo": {}}, previous,
    )

    assert queries == [
        "Star Fox game",
        "Star Fox game gameplay screenshot",
        "Star Fox game official artwork",
        "Star Fox game character scene",
        "Star Fox game environment screenshot",
    ]


def test_featured_search_continues_after_final_rejection(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    def candidate(url, candidate_id):
        return {
            "candidate_id": candidate_id,
            "direct_image_url": url,
            "source_page_url": f"https://source.test/{candidate_id}",
            "subject": "Test",
            "author": "Test",
            "license": "CC BY",
            "license_url": "https://license.test",
            "captured_at": "2026-01-01T00:00:00Z",
            "credit_text": "Crédito",
            "alt_text": "Test",
            "evidence": {"verdict": "deterministic_match", "score": 9},
        }

    queries = []

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        query = batch["posts"][0]["query"]
        queries.append(query)
        return {"posts": [{
            "reuse": [],
            "audit_candidates": [candidate(
                "https://cdn.test/feature-a.webp" if len(queries) == 1 else "https://cdn.test/feature-b.webp",
                f"feature-{len(queries)}",
            )],
        }]}

    def fake_execute(editorial, *_args, **_kwargs):
        item = editorial["media_plan"][0]
        if item["direct_image_url"].endswith("feature-a.webp"):
            return ([{"paragraph_index": 0, "status": "rejected", "detail": "vision_rejected"}], None, None)
        return ([{
            "media_id": 91,
            "media_url": item["direct_image_url"],
            "paragraph_index": 0,
            "featured": True,
            "status": "accepted",
        }], 91, "Crédito")

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Test"}])
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)

    previous = MediaProgress(required=0, featured=FeaturedProgress(FeaturedStatus.MISSING))
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115102, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test.</p>", "seo": {}},
        previous,
    )

    assert len(queries) == 2
    assert result.featured.status is FeaturedStatus.VALID
    assert result.featured.media_id == 91


def test_vision_rejected_featured_is_not_recovered_from_wordpress(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime
    import unicornio_editor.workflow as workflow

    normalize = mock.Mock(side_effect=AssertionError("rejected featured must not return"))
    search_calls = []
    monkeypatch.setattr(workflow, "_normalize_existing_featured", normalize)
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Test"}])

    def fake_resolve(*_args, **_kwargs):
        search_calls.append(True)
        return {"posts": [{"reuse": [], "audit_candidates": []}]}

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(runtime, "_execute_media_plan", lambda *_args, **_kwargs: ([], None, None))

    previous = MediaProgress(
        required=0,
        featured=FeaturedProgress(FeaturedStatus.VISION_REJECTED, None, None),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {
            "post_id": 115102,
            "title": "Test",
            "post": {"id": 115102, "featured_media": 777},
        },
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test.</p>", "seo": {}},
        previous,
    )

    normalize.assert_not_called()
    assert search_calls
    assert result.featured.status is FeaturedStatus.VISION_REJECTED
    assert result.featured.media_id is None


def test_media_resolver_uses_article_source_before_external_search(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.source_verify as source_verify
    import unicornio_editor.pipeline_v2.runtime as runtime

    source_candidate = {
        "candidate_id": "article-source-1",
        "direct_image_url": "https://cdn.test/article.webp",
        "source_page_url": "https://source.test/article",
        "author": "Source",
        "license": "Uso com crédito",
        "license_url": "https://source.test/article",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito da imagem: source.test",
        "alt_text": "Test",
        "evidence": {"verdict": "deterministic_match"},
    }

    monkeypatch.setattr(source_verify, "discover_article_source_candidates", lambda *_args, **_kwargs: [dict(source_candidate)])
    monkeypatch.setattr(cli, "_resolve_media_batch", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("external search must not run")))
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(runtime, "_execute_media_plan", lambda editorial, *_args, **_kwargs: ([{
        "status": "accepted",
        "media_id": 44,
        "media_url": "https://cdn.test/article.webp",
        "paragraph_index": editorial["media_plan"][0]["paragraph_index"],
        "alt_text": "Test",
        "credit_text": "Crédito",
        "featured": False,
    }], None, None))
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Test"}])

    previous = MediaProgress(required=1, featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"))
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "Test", "original_link": "https://source.test/article"},
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test.</p>", "seo": {}},
        previous,
    )
    assert result.accepted == 1
    assert result.inline[0].media_id == 44


def test_vision_input_failure_rejects_one_candidate_and_keeps_valid_candidate(monkeypatch, tmp_path):
    import unicornio_editor.media.vision_gate as vision_gate
    config = SimpleNamespace(
        vision_enabled=True,
        vision_api_key="test-key",
        vision_max_low=10,
        http_timeout=1,
        vision_base_url="https://vision.test/v1",
        vision_model="vision-test",
        vision_detail="low",
    )
    resolver = ProductionMediaResolver(object(), config, tmp_path)
    candidates = [
        {
            "candidate_id": "bad",
            "direct_image_url": "https://cdn.test/missing.webp",
            "subject": "Obra A",
            "role": "inline",
            "evidence": {"verdict": "ambiguous", "needs_vision": True, "score": 5},
        },
        {
            "candidate_id": "good",
            "direct_image_url": "https://cdn.test/good.webp",
            "subject": "Obra B",
            "role": "inline",
            "evidence": {"verdict": "ambiguous", "needs_vision": True, "score": 5},
        },
    ]

    def prepare(url, **_kwargs):
        if url.endswith("missing.webp"):
            raise vision_gate.VisionInputUnavailable("HTTP 404")
        return "data:image/png;base64,AAAA"

    monkeypatch.setattr(vision_gate, "prepare_vision_image_input", prepare)
    monkeypatch.setattr(
        vision_gate,
        "verify_image_subject_batch",
        lambda **_kwargs: {"good": {"verdict": "accept", "confidence": 0.97, "visual_type": "key_art"}},
    )
    resolver._resolve_ambiguous_vision(candidates, post_id=115025)

    assert candidates[0]["evidence"]["verdict"] == "vision_input_unavailable"
    assert candidates[0]["rejected_reason"] == "vision_input_unavailable"
    assert candidates[1]["evidence"]["verdict"] == "deterministic_match"
    telemetry = (tmp_path / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
    assert "vision_input_unavailable" in telemetry
    assert "data:image/png;base64,AAAA" not in telemetry
