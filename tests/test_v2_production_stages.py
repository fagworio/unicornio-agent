import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unicornio_editor.pipeline_v2.errors import StageError
from unicornio_editor.pipeline_v2.model import BlockerCode, FeaturedProgress, FeaturedStatus, InlineMedia, LifecycleState, MediaProgress, Outcome, Phase, RetryInfo, WorkState
from unicornio_editor.pipeline_v2.production_stages import ProductionComposeStage, ProductionEditorialStage, ProductionMediaStage, ProductionValidateStage
from unicornio_editor.pipeline_v2.runtime import WordPressWriterV2
from unicornio_editor.workflow import MediaFunnelInvariantError


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


def test_media_funnel_invariant_maps_to_internal_media_error(tmp_path):
    stage = ProductionMediaStage(
        object(),
        Config(),
        tmp_path,
        resolver=lambda *_args: (_ for _ in ()).throw(
            MediaFunnelInvariantError("media candidate conservation violated")
        ),
    )
    with pytest.raises(StageError) as error:
        stage({"post_id": 7}, SimpleNamespace(media=MediaProgress(required=1)), {})
    assert error.value.blocker is BlockerCode.INTERNAL_ERROR
    assert error.value.phase is Phase.MEDIA


def test_editorial_repairs_keyword_before_provider_rework(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    input_path.write_text('{"posts":[{"post_id":115002}]}', encoding="utf-8")
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.prepare_batch",
        lambda *_args: {"prepared": 1, "editorial_input": str(input_path)},
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "vazamentos de GTA 6"}],
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.generate_editorial_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider must not run")),
    )
    draft_dir = tmp_path / "backups" / "115002"
    draft_dir.mkdir(parents=True)
    draft = {
        "cleaned_html": "<p>Vazamentos sobre GTA 6 revelam novidades.</p>",
        "seo": {"title": "Vazamentos: GTA 6 ganham força", "focus_keyword": "termo antigo"},
        "media_plan": [],
    }
    (draft_dir / "editorial.draft.json").write_text(json.dumps(draft), encoding="utf-8")
    state = WorkState(
        phase=Phase.EDITORIAL,
        blocker=BlockerCode.TEXT_QUALITY,
        retry=RetryInfo(phase_attempts=3),
    )
    repaired = ProductionEditorialStage(object(), Config(), tmp_path)(
        {"post_id": 115002, "post": {"id": 115002, "title": {"raw": draft["seo"]["title"]}}},
        state,
    )
    assert repaired["seo"]["focus_keyword"] == "vazamentos de GTA 6"
    persisted = json.loads((draft_dir / "editorial.draft.json").read_text(encoding="utf-8"))
    assert persisted["seo"]["focus_keyword"] == "vazamentos de GTA 6"


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


def test_compose_does_not_repeat_existing_or_featured_inline_media(tmp_path):
    existing = "https://example.test/existing.webp"
    featured = "https://example.test/featured.webp"
    new = "https://example.test/new.webp"
    editorial = {
        "cleaned_html": f'<p>one</p><img src="{existing}" /><p>two</p><p>three</p><p>four</p>',
        "seo": {},
    }
    media = MediaProgress(
        required=2,
        inline=(
            InlineMedia(10, existing, 0, "Existing", "Crédito da imagem: Existing"),
            InlineMedia(11, featured, 3, "Featured", "Crédito da imagem: Featured"),
            InlineMedia(12, new, 2, "New", "Crédito da imagem: New"),
        ),
        featured=FeaturedProgress(FeaturedStatus.VALID, 11, featured),
    )
    candidate = ProductionComposeStage(Config(), tmp_path)(
        {"post_id": 12, "original_link": None}, editorial, media
    )
    content = candidate["content"]
    assert content.count(existing) == 1
    assert content.count(featured) == 0
    assert content.count(new) == 1


def test_writer_requires_inline_media_in_wordpress_readback(tmp_path):
    inline_url = "https://example.test/accepted.webp"

    class Client:
        def __init__(self):
            self.post = {
                "id": 13,
                "status": "pending",
                "content": {"raw": "old"},
                "featured_media": 0,
                "meta": {},
            }

        def get_post(self, _post_id):
            return self.post

        def update_post(self, _post_id, update):
            self.post.update(update)

    client = Client()
    backup = tmp_path / "backups" / "13"
    backup.mkdir(parents=True)
    (backup / "editorial.candidate.json").write_text(
        json.dumps({
            "content": f'<p>texto</p><img src="{inline_url}" />',
                        "editorial": {"cleaned_html": "<p>texto</p>", "seo": {"title": "Título", "meta_description": "Descrição editorial com contexto e informações essenciais para o leitor entender a notícia e acompanhar os principais pontos desta publicação.", "focus_keyword": "título"}},
                        "seo": {"title": "Título", "meta_description": "Descrição editorial com contexto e informações essenciais para o leitor entender a notícia e acompanhar os principais pontos desta publicação.", "focus_keyword": "título"},
            "featured_media": None,
        }),
        encoding="utf-8",
    )
    media = MediaProgress(
        required=1,
        inline=(InlineMedia(13, inline_url, 0, "Imagem", "Crédito da imagem: Fonte"),),
    )
    state = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
        media=media,
    )
    result = WordPressWriterV2(client, tmp_path).commit(13, {"post_id": 13}, state, Outcome.ready())
    assert result["readback"] is True


def test_writer_applies_accepted_inline_media_while_partial_and_is_idempotent(tmp_path):
    inline_url = "https://example.test/partial.webp"

    class Client:
        def __init__(self):
            self.post = {
                "id": 14,
                "status": "pending",
                "content": {"raw": "<p>texto</p>"},
                "featured_media": 0,
                "meta": {},
            }
            self.updates = []

        def get_post(self, _post_id):
            return self.post

        def update_post(self, _post_id, update):
            self.updates.append(update)
            self.post.update(update)

    client = Client()
    backup = tmp_path / "backups" / "14"
    backup.mkdir(parents=True)
    (backup / "editorial.candidate.json").write_text(
        json.dumps({"content": f'<p>texto</p><img src="{inline_url}" />'}),
        encoding="utf-8",
    )
    state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        relevance_approved=True,
        media=MediaProgress(
            required=2,
            inline=(InlineMedia(14, inline_url, 0, "Imagem", "Crédito da imagem: Fonte"),),
        ),
    )
    outcome = Outcome.retry(Phase.MEDIA, BlockerCode.INLINE_MISSING)

    first = WordPressWriterV2(client, tmp_path).commit(14, {"post_id": 14}, state, outcome)
    assert first["readback"] is True
    assert client.post["content"]["raw"].count(inline_url) == 1
    assert len(client.updates) == 1
    assert client.updates[0]["content"]["raw"].count(inline_url) == 1

    second = WordPressWriterV2(client, tmp_path).commit(14, {"post_id": 14}, state, outcome)
    assert second["readback"] is True
    assert len(client.updates) == 1
    assert client.post["content"]["raw"].count(inline_url) == 1


def test_writer_reports_partial_apply_readback_failure_as_technical_error(tmp_path):
    inline_url = "https://example.test/not-persisted.webp"

    class Client:
        def __init__(self):
            self.post = {
                "id": 15,
                "status": "pending",
                "content": {"raw": "<p>texto</p>"},
                "featured_media": 0,
                "meta": {},
            }

        def get_post(self, _post_id):
            return self.post

        def update_post(self, _post_id, update):
            self.post["meta"] = update.get("meta") or {}

    backup = tmp_path / "backups" / "15"
    backup.mkdir(parents=True)
    (backup / "editorial.candidate.json").write_text(
        json.dumps({"content": f'<p>texto</p><img src="{inline_url}" />'}),
        encoding="utf-8",
    )
    state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        relevance_approved=True,
        media=MediaProgress(
            required=2,
            inline=(InlineMedia(15, inline_url, 0, "Imagem", "Crédito da imagem: Fonte"),),
        ),
    )

    with pytest.raises(RuntimeError, match="inline media read-back mismatch"):
        WordPressWriterV2(Client(), tmp_path).commit(
            15,
            {"post_id": 15},
            state,
            Outcome.retry(Phase.MEDIA, BlockerCode.INLINE_MISSING),
        )


def test_validate_repairs_focus_keyword_even_with_media_failure(tmp_path, monkeypatch):
    seen_keywords = []
    candidate = {
        "content": "<p>Vazamentos sobre GTA 6 revelam novidades da produção.</p>",
        "editorial": {
            "cleaned_html": "<p>Vazamentos sobre GTA 6 revelam novidades da produção.</p>",
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
        lambda **_kwargs: [{"subject": "vazamentos de GTA 6"}],
    )
    candidate["editorial"]["seo"]["title"] = "Vazamentos: GTA 6 ganham força"
    candidate["seo"]["title"] = "Vazamentos: GTA 6 ganham força"
    post = {"id": 9, "status": "pending", "title": {"raw": "Vazamentos: GTA 6 ganham força"}, "meta": {}}
    result = ProductionValidateStage(object(), Config(), tmp_path)(
        {"post_id": 9, "post": post, "v2_state": None}, candidate
    )
    assert seen_keywords == ["termo antigo", "vazamentos de GTA 6"]
    assert result["passed"] is True
    assert '"focus_keyword": "vazamentos de GTA 6"' in (draft_dir / "editorial.draft.json").read_text(encoding="utf-8")


def test_validate_does_not_repair_focus_keyword_when_token_order_is_wrong(tmp_path, monkeypatch):
    candidate = {
        "content": "<p>GTA 6 vazamentos continuam sem confirmação oficial.</p>",
        "editorial": {
            "cleaned_html": "<p>GTA 6 vazamentos continuam sem confirmação oficial.</p>",
            "seo": {"title": "Vazamentos: GTA 6 ganham força", "focus_keyword": "termo antigo"},
        },
        "seo": {"title": "Vazamentos: GTA 6 ganham força", "focus_keyword": "termo antigo"},
    }
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.run_pre_publish_checklist",
        lambda **_: {"all_passed": False, "items": [{"name": "qualidade_texto", "status": "fail", "detail": "focus keyword must occur naturally"}]},
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "vazamentos de GTA 6"}],
    )
    result = ProductionValidateStage(object(), Config(), tmp_path)(
        {"post_id": 11, "post": {"id": 11, "status": "pending", "title": {"raw": "Vazamentos: GTA 6 ganham força"}, "meta": {}}, "v2_state": None},
        candidate,
    )
    assert result["passed"] is False
    assert candidate["seo"]["focus_keyword"] == "termo antigo"



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


def test_validate_does_not_resurrect_stale_wordpress_featured(tmp_path, monkeypatch):
    seen_featured = []

    def fake_checklist(**kwargs):
        seen_featured.append(kwargs["post"]["featured_media"])
        return {"all_passed": True, "items": []}

    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.run_pre_publish_checklist",
        fake_checklist,
    )
    candidate = {
        "content": "<p>Texto.</p>",
        "editorial": {"cleaned_html": "<p>Texto.</p>", "seo": {}},
        "seo": {},
        "featured_media": None,
    }
    result = ProductionValidateStage(object(), Config(), tmp_path)(
        {
            "post_id": 12,
            "post": {
                "id": 12,
                "status": "pending",
                "title": {"raw": "Test"},
                "meta": {},
                "featured_media": 777,
            },
            "v2_state": None,
        },
        candidate,
    )

    assert result["passed"] is True
    assert seen_featured == [None]
