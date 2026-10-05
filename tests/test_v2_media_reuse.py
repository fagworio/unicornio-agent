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
        return {"posts": [{"reuse": [{"url": "https://cdn.test/existing.webp", "source": "https://source.test/page", "media_id": 77}], "audit_candidates": []}]}

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
