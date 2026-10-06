from types import SimpleNamespace

from unicornio_editor.pipeline_v2.model import MediaProgress
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
            audit = [candidate("https://cdn.test/featured.webp", item["subject"], "featured")]
        elif "Nana" in item["query"]:
            audit = [candidate("https://cdn.test/inline.webp", item["subject"], "inline")]
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
    assert plan[1]["direct_image_url"] == "https://cdn.test/inline.webp"
    assert any("Nana anime" in query for query in calls)
    assert result.featured.status is runtime.FeaturedStatus.VALID
