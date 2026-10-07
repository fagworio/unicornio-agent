from types import SimpleNamespace

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

    seen = {}

    def fake_resolve(*_args, **_kwargs):
        return {"posts": [{"reuse": [{"url": "https://cdn.test/existing.webp", "source": "https://source.test/page", "media_id": 77, "author": "Test", "license": "CC BY", "license_url": "https://license.test", "captured_at": "2026-01-01T00:00:00Z", "credit_text": "Crédito", "alt_text": "Existing"}], "audit_candidates": []}]}

    def fake_validate(_client, editorial, **_kwargs):
        seen["plan"] = editorial["media_plan"]
        return {"valid": True, "rejected": []}

    def fake_execute(editorial, *_args, **_kwargs):
        return ([{"media_id": 77, "media_url": "https://cdn.test/existing.webp", "paragraph_index": 0, "alt_text": "existing", "credit_text": "Crédito da imagem: Existing", "featured": False}], None, None)

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "validate_media_plan", fake_validate)
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)
    previous = MediaProgress(required=2)
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "Test"}, SimpleNamespace(media=previous), {"cleaned_html": "<p>one</p>", "seo": {}}, previous
    )
    assert seen["plan"][0]["direct_image_url"] == "https://cdn.test/existing.webp"
    assert seen["plan"][0]["source_page_url"] == "https://source.test/page"
    assert result.accepted == 1


def test_media_resolver_keeps_featured_and_inline_roles_separate(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    calls = []
    seen = {}

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
        seen["plan"] = editorial["media_plan"]
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

    plan = seen["plan"]
    assert [item["is_featured"] for item in plan] == [True, False]
    assert plan[0]["direct_image_url"] == "https://cdn.test/featured.webp"
    assert plan[1]["direct_image_url"] == "https://cdn.test/featured-alt.webp"
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
