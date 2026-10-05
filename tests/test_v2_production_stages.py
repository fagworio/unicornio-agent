from pathlib import Path
from types import SimpleNamespace

from unicornio_editor.pipeline_v2.model import FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress
from unicornio_editor.pipeline_v2.production_stages import ProductionComposeStage, ProductionMediaStage, ProductionValidateStage


class Config:
    internal_links_enabled = False
    http_timeout = 1
    site_topics = ()
    remote_url_policy = "off"
    vision_enabled = False
    min_relevance_confidence = 0.8
    min_skip_confidence = 0.9
    max_media_search_attempts = 2
    max_rework_attempts = 3
    internal_links_enabled = False


def test_media_stage_reuses_existing_and_writes_manifest(tmp_path):
    existing = InlineMedia(10, "https://example.test/a.webp", 0, "A", "Crédito da imagem: A")
    stage = ProductionMediaStage(object(), Config(), tmp_path, resolver=lambda *_args: MediaProgress(
        required=2, inline=(existing,), featured=FeaturedProgress(FeaturedStatus.MISSING)
    ))
    result = stage({"post_id": 7}, SimpleNamespace(media=MediaProgress(required=2, inline=(existing,))), {})
    assert result.accepted == 1
    assert (tmp_path / "backups/7/editorial.partial.json").exists()


def test_compose_and_validate_are_real_adapters(tmp_path, monkeypatch):
    editorial = {
        "cleaned_html": "<p>one</p><p>two</p><p>three</p><p>four</p>",
        "seo": {},
    }
    media = MediaProgress(required=0)
    candidate = ProductionComposeStage(Config(), tmp_path)(
        {"post_id": 8, "original_link": None}, editorial, media
    )
    assert candidate["content"]
    assert (tmp_path / "backups/8/editorial.candidate.json").exists()

    class Client:
        pass

    post = {"id": 8, "status": "pending", "title": {"raw": "Test"}, "meta": {}}
    monkeypatch.setattr("unicornio_editor.pipeline_v2.production_stages.run_pre_publish_checklist", lambda **_: {"all_passed": False, "items": [{"name": "x", "status": "fail"}]})
    validation = ProductionValidateStage(Client(), Config(), tmp_path)(
        {"post_id": 8, "post": post, "v2_state": None}, candidate
    )
    assert set(validation) >= {"passed", "failures", "checklist"}
