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


def test_validate_repairs_focus_keyword_even_with_media_failure(tmp_path, monkeypatch):
    seen_keywords = []
    candidate = {
        "content": "<p>Bailarina acompanha uma história de ação.</p>",
        "editorial": {
            "cleaned_html": "<p>Bailarina acompanha uma história de ação.</p>",
            "seo": {"title": "Bailarina: novo filme", "focus_keyword": "termo antigo"},
        },
        "seo": {"title": "Bailarina: novo filme", "focus_keyword": "termo antigo"},
    }

    def fake_checklist(**kwargs):
        seen_keywords.append(kwargs["editorial"]["seo"]["focus_keyword"])
        if len(seen_keywords) == 1:
            return {
                "all_passed": False,
                "items": [
                    {"name": "qualidade_texto", "status": "fail", "detail": "focus keyword must occur naturally"},
                    {"name": "imagens_no_corpo", "status": "fail", "detail": "inline media missing"},
                ],
            }
        return {"all_passed": True, "items": []}

    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.run_pre_publish_checklist",
        fake_checklist,
    )
    draft_dir = tmp_path / "backups" / "9"
    draft_dir.mkdir(parents=True)
    (draft_dir / "editorial.draft.json").write_text(
        '{"cleaned_html":"<p>Bailarina acompanha uma história de ação.</p>","seo":{"focus_keyword":"termo antigo"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "Bailarina"}],
    )
    post = {"id": 9, "status": "pending", "title": {"raw": "Bailarina: novo filme"}, "meta": {}}
    result = ProductionValidateStage(object(), Config(), tmp_path)(
        {"post_id": 9, "post": post, "v2_state": None}, candidate
    )
    assert seen_keywords == ["termo antigo", "Bailarina"]
    assert result["passed"] is True
    assert '"focus_keyword": "Bailarina"' in (draft_dir / "editorial.draft.json").read_text(encoding="utf-8")


def test_validate_persists_dash_repair_in_canonical_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.run_pre_publish_checklist",
        lambda **_: {"all_passed": True, "items": []},
    )
    draft_dir = tmp_path / "backups" / "10"
    draft_dir.mkdir(parents=True)
    (draft_dir / "editorial.draft.json").write_text(
        '{"cleaned_html":"<p>Bailarina — ação.</p>","seo":{}}',
        encoding="utf-8",
    )
    candidate = {
        "content": "<p>Bailarina — ação.</p>",
        "editorial": {"cleaned_html": "<p>Bailarina — ação.</p>", "seo": {}},
        "seo": {},
    }
    result = ProductionValidateStage(object(), Config(), tmp_path)(
        {"post_id": 10, "post": {"id": 10, "status": "pending", "title": {"raw": "Bailarina"}, "meta": {}}, "v2_state": None},
        candidate,
    )
    assert result["passed"] is True
    draft = (draft_dir / "editorial.draft.json").read_text(encoding="utf-8")
    assert "—" not in draft


def test_validate_uses_candidate_featured_before_wordpress_projection(tmp_path, monkeypatch):
    seen_featured = []

    def fake_checklist(**kwargs):
        seen_featured.append(kwargs["post"]["featured_media"])
        return {"all_passed": True, "items": []}

    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.run_pre_publish_checklist",
        fake_checklist,
    )
    candidate = {
        "content": "<p>Bailarina — ação.</p>",
        "editorial": {"cleaned_html": "<p>Bailarina — ação.</p>", "seo": {}},
        "seo": {},
        "featured_media": 123,
    }
    result = ProductionValidateStage(object(), Config(), tmp_path)(
        {"post_id": 11, "post": {"id": 11, "status": "pending", "title": {"raw": "Bailarina"}, "meta": {}, "featured_media": 0}, "v2_state": None},
        candidate,
    )
    assert result["passed"] is True
    assert seen_featured == [123]
    persisted = tmp_path / "backups" / "11" / "editorial.candidate.json"
    assert persisted.is_file()
    assert "—" not in persisted.read_text(encoding="utf-8")
