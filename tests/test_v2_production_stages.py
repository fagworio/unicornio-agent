import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unicornio_editor.pipeline_v2.errors import StageError
from unicornio_editor.pipeline_v2.model import BlockerCode, FeaturedProgress, FeaturedStatus, InlineMedia, LifecycleState, MediaProgress, Outcome, OutcomeType, Phase, RetryInfo, WorkState
from unicornio_editor.pipeline_v2.production_stages import (
    ProductionComposeStage,
    ProductionEditorialStage,
    ProductionMediaStage,
    ProductionValidateStage,
    _repair_focus_keyword_draft,
)
from unicornio_editor.pipeline_v2.production import ProductionContextLoader
from unicornio_editor.pipeline_v2.runtime import WordPressWriterV2, _embedded_media_urls
from unicornio_editor.media.registry import (
    connect as connect_media_registry,
    invalidate_v2_media,
    record_v2_observation,
)
from unicornio_editor.workflow import MediaFunnelInvariantError, _resolve_seo_from_post


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


def _verified_inline(media_id, url, slot, *, decision="DIFFERENT"):
    digest = f"{media_id:064x}"
    return InlineMedia(
        media_id,
        url,
        slot,
        alt_text=f"Imagem {media_id}",
        credit_text=f"Crédito da imagem: Fonte {media_id}",
        sha256=digest,
        phash=f"{media_id:016x}",
        visual_group_id=f"visual:{media_id:024x}",
        visual_verification={"decision": decision},
    )


def test_media_stage_reuses_existing_and_writes_manifest(tmp_path):
    existing = InlineMedia(10, "https://example.test/a.webp", 0, "A", "Crédito da imagem: A")
    stage = ProductionMediaStage(object(), Config(), tmp_path, resolver=lambda *_args: MediaProgress(
        required=2, inline=(existing,), featured=FeaturedProgress(FeaturedStatus.MISSING)
    ))
    result = stage({"post_id": 7}, SimpleNamespace(media=MediaProgress(required=2, inline=(existing,))), {})
    assert result.accepted == 1
    assert (tmp_path / "backups/7/editorial.partial.json").exists()


@pytest.mark.parametrize("title", ["Título string", {"raw": "Título bruto"}, {"rendered": "Título renderizado"}])
def test_production_context_loader_normalizes_wordpress_title(tmp_path, title):
    context = ProductionContextLoader(object(), tmp_path).load({
        "id": 115230, "title": title, "content": "<p>texto</p>", "meta": {},
    })
    assert context["title"].startswith("Título")


def test_production_context_loader_rejects_invalid_title_type(tmp_path):
    with pytest.raises(ValueError, match="title"):
        ProductionContextLoader(object(), tmp_path).load({"id": 115232, "title": ["ruim"]})


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


def test_media_stage_logs_contract_error_with_run_identity_and_redacted_traceback(tmp_path):
    stage = ProductionMediaStage(
        object(), Config(), tmp_path,
        resolver=lambda *_args: (_ for _ in ()).throw(TypeError("token=must-not-leak")),
    )
    with pytest.raises(StageError) as error:
        stage(
            {"post_id": 115152, "v2_run_id": "run-contract"},
            SimpleNamespace(media=MediaProgress(required=1)),
            {},
        )
    assert error.value.blocker is BlockerCode.INTERNAL_ERROR
    event = json.loads((tmp_path / "work/telemetry.jsonl").read_text(encoding="utf-8"))
    assert event["error_class"] == "contract"
    assert event["post_id"] == 115152 and event["run_id"] == "run-contract"
    assert event["file"] == "pipeline_v2/production_stages.py"
    assert event["field"] == "ProductionMediaStage"
    assert event["exception_type"] == "TypeError"
    assert "[redacted sensitive traceback line]" in event["traceback"]
    assert "must-not-leak" not in json.dumps(event)


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


def test_repair_keyword_uses_title_ngrams_when_subjects_are_too_narrow():
    editorial = {
        "title": "Emular jogos de PC no Android: desempenho e armazenamento",
        "cleaned_html": "<p>Testar jogos de PC no celular é uma surpresa.</p>",
        "seo": {
            "title": "Emular jogos de PC no Android: desempenho e armazenamento",
            "focus_keyword": "emular jogos de PC no Android",
        },
    }
    repaired = _repair_focus_keyword_draft(editorial, {"title": editorial["title"]})
    assert repaired is not None
    assert repaired["seo"]["focus_keyword"] == "jogos de PC"


def test_rank_math_keyword_is_not_inherited_when_absent_from_body():
    post = {
        "title": {"raw": "Emular jogos de PC no Android: desempenho e armazenamento"},
        "content": {"raw": "<p>Testar jogos de PC no celular é uma surpresa.</p>"},
        "meta": {
            "rank_math_title": "Emular jogos de PC no Android: desempenho e armazenamento",
            "rank_math_description": "Uma descrição suficientemente longa e detalhada sobre o desempenho e o armazenamento em celulares Android para o artigo editorial.",
            "rank_math_focus_keyword": "emular jogos de PC no Android",
        },
    }
    resolved = _resolve_seo_from_post(post)
    assert resolved["focus_keyword"] == "jogos de PC"


def test_editorial_repairs_keyword_in_fresh_provider_output_before_media(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    input_path.write_text('{"posts":[{"post_id":115003}]}', encoding="utf-8")
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
        lambda *_args, **_kwargs: {"output": "generated.json"},
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.load_editorial_batch",
        lambda *_args: {"items": [{"post_id": 115003, "editorial": {
            "title": "Vazamentos: GTA 6 ganham força",
            "cleaned_html": "<p>Vazamentos sobre GTA 6 revelam novidades.</p>",
            "seo": {"title": "Vazamentos: GTA 6 ganham força", "focus_keyword": "termo ausente"},
            "site_relevance": {"decision": "process"},
        }}]},
    )
    monkeypatch.setattr("unicornio_editor.pipeline_v2.production_stages.resolve_editorial_defaults", lambda value, _post: value)
    monkeypatch.setattr("unicornio_editor.pipeline_v2.production_stages.validate_editorial", lambda value, **_kwargs: value)
    editorial = ProductionEditorialStage(object(), SimpleNamespace(
        editorial_api_key="x", editorial_base_url="x", editorial_model="x",
        http_timeout=1, min_relevance_confidence=0.8,
    ), tmp_path)(
        {"post_id": 115003, "post": {"id": 115003, "title": {"raw": "Vazamentos GTA 6"}}},
        WorkState(phase=Phase.RELEVANCE),
    )
    assert editorial["seo"]["focus_keyword"] == "vazamentos de GTA 6"


def test_compose_and_validate_are_real_adapters(tmp_path, monkeypatch):
    editorial = {
        "cleaned_html": "<p>one</p><p>two</p><p>three</p><p>four</p>",
        "seo": {
            "title": "Título SEO",
            "meta_description": "Descrição editorial com contexto suficiente para validar o fluxo seguro de publicação e confirmar os dados principais.",
            "focus_keyword": "título",
        },
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


def test_localization_context_is_not_sent_back_to_strict_editorial_schema(tmp_path, monkeypatch):
    captured = {}

    def fake_checklist(**kwargs):
        captured["editorial"] = kwargs["editorial"]
        return {"all_passed": True, "items": []}

    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.run_pre_publish_checklist",
        fake_checklist,
    )
    candidate = {
        "content": "<p>Texto em português.</p>",
        "editorial": {
            "site_relevance": {"decision": "process", "confidence": 0.95, "reason": "ok", "matched_topics": ["games"]},
            "cleaned_html": "<p>Texto em português.</p>",
            "media_plan": [],
            "needs_trailer": False,
            "trailer_url": None,
            "game_name": None,
            "localization": {"required": False, "source_language": {}},
        },
        "featured_media": None,
        "media": {},
    }
    result = ProductionValidateStage(object(), Config(), tmp_path)(
        {"post_id": 91, "post": {"id": 91, "status": "pending", "title": {"raw": "Teste"}, "meta": {}}},
        candidate,
    )
    assert result["passed"] is True
    assert "localization" not in captured["editorial"]
    assert "localization" not in candidate["editorial"]


def test_compose_does_not_repeat_existing_or_featured_inline_media(tmp_path):
    existing = "https://example.test/existing.webp"
    featured = "https://example.test/featured.webp"
    new = "https://example.test/new.webp"
    paragraph = "Texto editorial com contexto suficiente para explicar a notícia ao leitor interessado."
    editorial = {
        "cleaned_html": (
            f'<p>{paragraph}</p><img src="{existing}" />'
            f'<figure><img src="{featured}" /><p>Contexto independente da imagem.</p></figure>'
            '<p>Crédito editorial independente.</p>'
            f"<p>{paragraph}</p><p>{paragraph}</p><p>{paragraph}</p>"
        ),
        "seo": {},
    }
    media = MediaProgress(
        required=2,
        inline=(
            _verified_inline(10, existing, 0),
            _verified_inline(11, featured, 3),
            _verified_inline(12, new, 2),
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
    assert "Contexto independente da imagem." in content
    assert "Crédito editorial independente." in content


def test_compose_plans_four_accepted_images_against_current_paragraphs(tmp_path):
    editorial = {
        "cleaned_html": "".join(
                f"<p>Parágrafo {index} com contexto editorial suficiente sobre videogame e seus detalhes principais.</p>"
            for index in range(13)
        ),
        "seo": {},
    }
    media = MediaProgress(
        required=4,
        inline=tuple(
                _verified_inline(100 + index, f"https://example.test/{index}.webp", 100 + index)
            for index in range(4)
        ),
    )
    candidate = ProductionComposeStage(Config(), tmp_path)(
        {"post_id": 16, "original_link": None}, editorial, media
    )
    content = candidate["content"]
    assert content.count("[caption id=\"\" align=\"aligncenter\"") == 4
    assert all(url in content for url in (f"https://example.test/{index}.webp" for index in range(4)))


def test_compose_routes_insufficient_paragraphs_to_structured_editorial_error(tmp_path):
    editorial = {
        "cleaned_html": "".join(
            "<p>Parágrafo com contexto editorial suficiente para explicar os detalhes conhecidos da notícia.</p>"
            for _ in range(4)
        ),
        "seo": {},
    }
    media = MediaProgress(
        required=4,
        inline=tuple(
                _verified_inline(200 + index, f"https://example.test/{index}.webp", 200 + index)
            for index in range(4)
        ),
    )
    with pytest.raises(StageError, match="media_insertion:insufficient_paragraph_slots") as error:
        ProductionComposeStage(Config(), tmp_path)(
            {"post_id": 17, "original_link": None}, editorial, media
        )
    assert error.value.blocker is BlockerCode.STRUCTURE
    assert error.value.phase is Phase.EDITORIAL
    assert "requested=4" in str(error.value)
    assert "available=1" in str(error.value)


def test_compose_reconciles_confirmed_duplicate_before_candidate_creation(tmp_path):
    duplicate_url = "https://example.test/duplicate.webp"
    distinct_url = "https://example.test/distinct.webp"
    body = "<p>Contexto editorial detalhado explica o jogo, a notícia e os principais acontecimentos recentes aos leitores.</p>"
    editorial = {
        "cleaned_html": (
            f'<figure><img src="{duplicate_url}"><figcaption>Legenda antiga</figcaption></figure>'
            + body * 8
        ),
        "seo": {},
    }
    duplicate = InlineMedia(
        21, duplicate_url, 0,
        sha256="b" * 64, phash="2" * 16, visual_group_id="visual:" + "b" * 24,
        visual_verification={"decision": "SAME_ART_CROP", "duplicate_of": "20"},
    )
    distinct = _verified_inline(22, distinct_url, 3)
    featured = FeaturedProgress(
        FeaturedStatus.VALID,
        20,
        "https://example.test/featured.webp",
        "a" * 64,
        "1" * 16,
        "visual:" + "a" * 24,
        {"decision": "INITIAL"},
    )
    media = MediaProgress(required=2, inline=(duplicate, distinct), featured=featured)

    candidate = ProductionComposeStage(Config(), tmp_path)(
        {"post_id": 22, "title": "Notícia", "original_link": None}, editorial, media,
    )

    assert duplicate_url not in candidate["content"]
    assert "Legenda antiga" not in candidate["content"]
    assert distinct_url in candidate["content"]
    accepted_ids = [row["media_id"] for row in candidate["media"]["inline"]["accepted"]]
    assert accepted_ids == [22]


def test_writer_requires_inline_media_in_wordpress_readback(tmp_path):
    inline_url = "https://example.test/accepted.webp"

    class Client:
        def __init__(self):
            self.post = {
                "id": 13,
                "status": "pending",
                "content": {"raw": "old"},
                "featured_media": 0,
                "meta": {
                    "_hermes_human_reopened_at": "2030-01-01T00:00:00+00:00",
                    "_hermes_human_reopened_from": "awaiting_human",
                },
            }
            self.updates = []

        def get_post(self, _post_id):
            return self.post

        def update_post(self, _post_id, update):
            self.updates.append(update)
            if "meta" in update:
                assert "_hermes_human_reopened_at" not in update["meta"]
                assert "_hermes_human_reopened_from" not in update["meta"]
                self.post["meta"].update(update["meta"])
            self.post.update({key: value for key, value in update.items() if key != "meta"})

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
    assert client.post["meta"]["_hermes_human_reopened_from"] == "awaiting_human"
    assert all(
        key not in update.get("meta", {})
        for update in client.updates
        for key in ("_hermes_human_reopened_at", "_hermes_human_reopened_from")
    )


def test_writer_keeps_ready_markers_unwritten_when_featured_media_readback_diverges(tmp_path):
    inline_url = "https://example.test/inline.webp"

    class Client:
        def __init__(self):
            self.post = {
                "id": 130,
                "status": "pending",
                "content": {"raw": "<p>antigo</p>"},
                "featured_media": 0,
                "meta": {"_hermes_state": "partial"},
            }

        def get_post(self, _post_id):
            return self.post

        def update_post(self, _post_id, update):
            # Simulate WordPress accepting the request but ignoring the
            # featured attachment update.
            self.post.update({key: value for key, value in update.items() if key != "featured_media"})

    backup = tmp_path / "backups" / "130"
    backup.mkdir(parents=True)
    (backup / "editorial.candidate.json").write_text(json.dumps({
        "content": f'<p>texto</p><img src="{inline_url}" />',
        "featured_media": 99,
        "title": "Título",
        "seo": {
            "title": "Título SEO",
            "meta_description": "Descrição editorial com contexto suficiente para validar o fluxo seguro de publicação e confirmar os dados principais. Extra.",
            "focus_keyword": "título",
        },
    }), encoding="utf-8")
    state = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
        media=MediaProgress(
            required=1,
            inline=(InlineMedia(13, inline_url, 0),),
            featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://example.test/featured.webp"),
        ),
    )

    record_v2_observation(
        tmp_path,
        post_id=130,
        run_id="failed-readback-130",
        state=state,
        phase="validate",
        outcome="ready_candidate",
        applied=False,
    )

    with pytest.raises(RuntimeError, match="featured media read-back mismatch"):
        WordPressWriterV2(Client(), tmp_path).commit(130, {"post_id": 130}, state, Outcome.ready())

    journal = json.loads((tmp_path / "work/v2-journal/130.json").read_text())
    assert journal["status"] == "media_readback_failed"
    assert journal["media_readback_status"] == "failed"
    assert {row["destination"] for row in journal["apply_media_records"]} == {"apply_readback_failed"}
    assert {row["destination"] for row in journal["media_records"]} == {"apply_readback_failed"}
    with connect_media_registry(tmp_path) as db:
        usages = db.execute(
            "SELECT status FROM media_usage WHERE post_id=?", (130,),
        ).fetchall()
    assert usages and {row[0] for row in usages} == {"accepted"}


def test_writer_rejects_unexpected_inline_media_when_v2_contract_is_empty(tmp_path):
    class Client:
        def __init__(self):
            self.post = {
                "id": 131,
                "status": "pending",
                "content": {"raw": "<p>antigo</p>"},
                "featured_media": 0,
                "meta": {},
            }
            self.updates = []

        def get_post(self, _post_id):
            return self.post

        def update_post(self, _post_id, update):
            self.updates.append(update)
            self.post.update({key: value for key, value in update.items() if key != "meta"})
            if "meta" in update:
                self.post["meta"].update(update["meta"])

    featured = FeaturedProgress(
        FeaturedStatus.VALID,
        99,
        "https://example.test/featured.webp",
        "f" * 64,
        "f" * 16,
        "visual:" + "f" * 24,
        {"decision": "INITIAL"},
    )
    media = MediaProgress(required=2, inline=(), featured=featured)
    backup = tmp_path / "backups" / "131"
    backup.mkdir(parents=True)
    (backup / "editorial.candidate.json").write_text(json.dumps({
        "content": '<p>Texto editorial.</p><img src="https://example.test/unexpected.webp" />',
        "editorial": {"cleaned_html": "<p>Texto editorial.</p>", "seo": {
            "title": "Título editorial",
            "meta_description": "Descrição editorial com contexto para validar o candidato, confirmar os dados principais e assegurar a persistência segura do estado READY durante o readback.",
            "focus_keyword": "título",
        }},
        "seo": {
            "title": "Título editorial",
            "meta_description": "Descrição editorial com contexto para validar o candidato, confirmar os dados principais e assegurar a persistência segura do estado READY durante o readback.",
            "focus_keyword": "título",
        },
        "featured_media": 99,
        "media": media.to_dict(),
    }), encoding="utf-8")
    state = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
        media=media,
    )

    client = Client()
    with pytest.raises(RuntimeError, match="unexpected inline media"):
        WordPressWriterV2(client, tmp_path).commit(131, {"post_id": 131}, state, Outcome.ready())
    assert client.updates == []
    assert client.post["content"] == {"raw": "<p>antigo</p>"}
    assert client.post["featured_media"] == 0


def test_writer_persists_only_readback_confirmed_candidate_media_in_ready_state(tmp_path):
    class Client:
        def __init__(self):
            self.post = {
                "id": 132,
                "status": "pending",
                "content": {"raw": "<p>conteúdo antigo</p>"},
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
    backup = tmp_path / "backups" / "132"
    backup.mkdir(parents=True)
    featured = FeaturedProgress(
        FeaturedStatus.VALID,
        100,
        "https://example.test/featured.webp",
        "f" * 64,
        "f" * 16,
        "visual:" + "f" * 24,
        {"decision": "INITIAL"},
    )
    accepted = tuple(
        _verified_inline(media_id, f"https://example.test/{media_id}.webp", slot)
        for media_id, slot in zip((201, 202, 203, 204), (0, 3, 6, 9))
    )
    duplicate = InlineMedia(
        200,
        "https://example.test/duplicate.webp",
        12,
        sha256=accepted[0].sha256,
        phash=accepted[0].phash,
        visual_group_id=accepted[0].visual_group_id,
        visual_verification={"decision": "SAME_IMAGE", "duplicate_of": "201"},
    )
    media = MediaProgress(required=4, inline=(duplicate, *accepted), featured=featured)
    candidate_media = MediaProgress(required=4, inline=accepted, featured=featured)
    content = "<p>Texto editorial.</p>" + "".join(
        f'<img src="{item.media_url}" />' for item in accepted
    )
    (backup / "editorial.candidate.json").write_text(json.dumps({
        "content": content,
        "editorial": {
            "cleaned_html": content,
            "seo": {
                "title": "Título", "meta_description": "Descrição editorial com contexto suficiente para validar a aprovação do candidato e confirmar todos os dados relevantes da publicação.",
                "focus_keyword": "título",
            },
        },
        "seo": {
            "title": "Título", "meta_description": "Descrição editorial com contexto suficiente para validar a aprovação do candidato e confirmar todos os dados relevantes da publicação.",
            "focus_keyword": "título",
        },
        "featured_media": featured.media_id,
        "media": candidate_media.to_dict(),
    }), encoding="utf-8")
    proposed = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
        media=media,
    )

    result = WordPressWriterV2(client, tmp_path).commit(
        132, {"post_id": 132}, proposed, Outcome.ready()
    )

    persisted = WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"]))
    assert result["media_readback"] is True
    assert result["media_application_status"] == "confirmed"
    assert result["html_readback_status"] == "confirmed"
    assert [item.media_id for item in persisted.media.inline] == [201, 202, 203, 204]
    assert result["applied_inline_ids"] == [201, 202, 203, 204]
    assert len(_embedded_media_urls(client.post["content"]["raw"])) == 4
    assert "meta" not in client.updates[0]
    journal = json.loads((tmp_path / "work/v2-journal/132.json").read_text())
    assert journal["status"] == "committed"
    assert journal["readback"] is True
    assert journal["media_application_status"] == "confirmed"
    assert journal["html_readback_status"] == "confirmed"
    assert [row["media_id"] for row in journal["media_records"] if row["role"] == "inline"] == [200, 201, 202, 203, 204]
    assert next(row for row in journal["media_records"] if row["media_id"] == 200)["destination"] == "rejected_duplicate"
    assert all(
        row["destination"] == "applied_readback_confirmed"
        for row in journal["media_records"]
        if row["media_id"] != 200
    )
    assert all(row["readback_status"] == "confirmed" for row in journal["apply_media_records"])
    assert all(
        row["destination"] == "applied_readback_confirmed"
        for row in journal["apply_media_records"]
    )
    record_v2_observation(
        tmp_path,
        post_id=132,
        run_id="run-132",
        state=proposed,
        phase="validate",
        outcome="started",
    )
    invalidate_v2_media(tmp_path, post_id=132, media_ids=[200], role="inline")
    record_v2_observation(
        tmp_path,
        post_id=132,
        run_id="run-132",
        state=persisted,
        phase="validate",
        outcome="ready",
        applied=True,
    )
    with connect_media_registry(tmp_path) as db:
        usages = db.execute(
            "SELECT wordpress_media_id, role, status, identity_evidence FROM media_usage WHERE post_id=? ORDER BY wordpress_media_id",
            (132,),
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in usages] == [
        (100, "featured", "applied"),
        (200, "inline", "invalidated"),
        (201, "inline", "applied"),
        (202, "inline", "applied"),
        (203, "inline", "applied"),
        (204, "inline", "applied"),
    ]
    duplicate_evidence = json.loads(usages[1][3])
    assert duplicate_evidence["decision"] == "SAME_IMAGE"


def test_v2_media_to_ready_uses_existing_inline_and_simulated_featured(tmp_path, monkeypatch):
    """Run real V2 compose/validate/writer stages with only WP acquisition mocked."""
    from unicornio_editor.config import Config as RuntimeConfig
    from unicornio_editor.pipeline_v2.runner import PipelineRunner

    post_id = 115286
    inline = (
        _verified_inline(115325, "https://cdn.test/xbox-live-console.webp", 0),
        _verified_inline(115326, "https://cdn.test/xbox-live-interface.webp", 4),
    )
    new_featured = FeaturedProgress(
        FeaturedStatus.VALID, 115288, "https://wp.test/xbox-live-featured.webp",
        "f" * 64, "f" * 16, "visual:" + "f" * 24,
        {"decision": "INITIAL"},
    )
    prior_media = MediaProgress(
        required=2, inline=inline,
        # WP had an attachment, but the V2 state had no accepted featured.
        featured=FeaturedProgress(FeaturedStatus.MISSING),
    )
    initial = WorkState(
        state=LifecycleState.PENDING, phase=Phase.MEDIA,
        relevance_approved=True, media=prior_media,
    )
    paragraphs = [
        "A Xbox Live anunciou mudanças importantes para jogadores e famílias. "
        "A atualização amplia as ferramentas de segurança e mantém as opções atuais.",
        "Segundo a empresa, as novas salvaguardas podem ser configuradas por cada conta.",
        "As medidas chegam gradualmente e incluem avisos mais claros durante o uso.",
        "A plataforma também publicou orientações para responsáveis e administradores.",
        "Jogadores poderão consultar os controles diretamente nas configurações da conta.",
        "A mudança faz parte de um conjunto de melhorias anunciado para este ano.",
        "A empresa informou que os recursos serão distribuídos em etapas para os usuários.",
    ]
    editorial = {
        "title": "Xbox Live anuncia novas salvaguardas para jogadores",
        "site_relevance": {
            "decision": "process", "confidence": 0.99,
            "reason": "assunto editorial compatível", "matched_topics": ["games"],
        },
        "cleaned_html": (
            f"<p>{paragraphs[0]}</p>"
            '<figure><img src="https://cdn.test/xbox-live-console.webp" '
            'alt="Xbox Live no console" width="1200" height="800" />'
            "<figcaption>Crédito da imagem: Fonte. Licença CC BY 4.0 "
            "(https://creativecommons.org/licenses/by/4.0/).</figcaption></figure>"
            + "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs[1:4])
            + '<figure><img src="https://cdn.test/xbox-live-interface.webp" '
            'alt="Interface do Xbox Live" width="1200" height="800" />'
            "<figcaption>Crédito da imagem: Fonte. Licença CC BY 4.0 "
            "(https://creativecommons.org/licenses/by/4.0/).</figcaption></figure>"
            + "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs[4:])
        ),
        "seo": {
            "title": "Xbox Live anuncia novas salvaguardas",
            "meta_description": (
                "Xbox Live amplia as salvaguardas e apresenta novas opções de segurança "
                "para jogadores, famílias e administradores de contas."
            ),
            "focus_keyword": "Xbox Live",
        },
        "media_plan": [], "needs_trailer": False, "trailer_url": None,
        "game_name": "Xbox Live",
    }
    config = RuntimeConfig(
        "wordpress", "http://wp.test", "http://wp.test/wp-json/wp/v2",
        dry_run=False, internal_links_enabled=False, publish_enabled=False,
        min_relevance_confidence=0.8,
    )

    class Store:
        def __init__(self):
            self.state = initial

        def load(self, _post_id):
            return self.state

        def commit(self, _post_id, state):
            self.state = state

    class Client:
        def __init__(self):
            self.post = {
                "id": post_id, "status": "pending",
                "title": {"raw": editorial["title"]},
                "content": {"raw": "conteúdo anterior"},
                "featured_media": 115287, "meta": {},
            }
            self.updates = []

        def get_post(self, _post_id):
            return self.post

        def get_media(self, media_id):
            urls = {
                115325: inline[0].media_url,
                115326: inline[1].media_url,
                115288: new_featured.media_url,
                115287: "https://wp.test/115287.webp",
            }
            return {
                "id": media_id,
                "source_url": urls[media_id],
                "title": {"rendered": "Xbox Live imagem editorial"},
                "alt_text": "Xbox Live imagem editorial",
                "caption": {"rendered": "Crédito da imagem: Fonte"},
                "media_details": {
                    "width": 1280,
                    "height": 720 if media_id == 115288 else 800,
                },
            }

        def update_post(self, _post_id, update):
            self.updates.append(update)
            self.post.update({key: value for key, value in update.items() if key != "content"})
            if isinstance(update.get("content"), dict):
                self.post.setdefault("content", {})["raw"] = update["content"]["raw"]
            if isinstance(update.get("meta"), dict):
                self.post.setdefault("meta", {}).update(update["meta"])
            return self.post

    store, client = Store(), Client()
    draft_dir = tmp_path / "backups" / str(post_id)
    draft_dir.mkdir(parents=True)
    (draft_dir / "editorial.draft.json").write_text(json.dumps(editorial), encoding="utf-8")
    media_stage = ProductionMediaStage(
        client, config, tmp_path,
        resolver=lambda _context, _state, _editorial, previous: MediaProgress(
            required=2, inline=previous.inline, featured=new_featured,
            search=previous.search, enrichment_round=previous.enrichment_round + 1,
        ),
    )
    stages = {
        "editorial": lambda _context, _state: editorial,
        "media": media_stage,
        "compose": ProductionComposeStage(config, tmp_path),
        "validate": ProductionValidateStage(client, config, tmp_path),
    }

    # URLs are deliberately synthetic. The checklist still executes every
    # gate; only its optional remote pHash fetch is replaced by deterministic,
    # distinct test fingerprints.
    monkeypatch.setattr(
        "unicornio_editor.media.visual_hash.image_hashes",
        lambda urls: {url: f"{index + 1:016x}" for index, url in enumerate(urls)},
    )
    # Trailer discovery is an external YouTube lookup unrelated to this
    # media-to-READY contract. Keep the integration test deterministic and
    # avoid waiting on a real socket when the network is unavailable.
    monkeypatch.setattr(
        "unicornio_editor.workflow.find_cached_game_trailer_with_status",
        lambda *args, **kwargs: (None, "official_not_found"),
    )
    outcome = PipelineRunner(store, stages, config=config).run_one(
        post_id,
        {
            "post_id": post_id, "title": editorial["title"],
            "post": client.post, "editorial": editorial, "root": tmp_path,
        },
    )

    assert outcome.type is OutcomeType.READY, outcome
    assert store.state.state is LifecycleState.READY
    candidate = json.loads(
        (tmp_path / "backups" / str(post_id) / "editorial.candidate.json").read_text()
    )
    content = candidate["content"]
    for item in inline:
        assert content.count(item.media_url) == 1
    assert candidate["featured_media"] == 115288
    assert content.count("xbox-live-featured.webp") == 0

    write = WordPressWriterV2(client, tmp_path).commit(
        post_id,
        {"post_id": post_id, "post": client.post},
        store.state,
        Outcome.ready(),
    )
    assert write["media_readback"] is True
    assert write["ready_hash"]
    assert client.post["status"] == "pending"
    assert client.post["featured_media"] == 115288
    assert client.post["meta"]["_hermes_ready_hash"] == write["ready_hash"]
    assert client.post["meta"].get("_hermes_ready_manifest")
    readback_content = client.post["content"]["raw"]
    assert all(readback_content.count(item.media_url) == 1 for item in inline)
    assert not any(update.get("status") == "publish" for update in client.updates)

    # Mirror the runtime's post-readback observation boundary: accepted assets
    # become applied only after the writer confirms the WordPress readback.
    record_v2_observation(
        tmp_path,
        post_id=post_id,
        run_id="ready-readback-115286",
        state=store.state,
        phase="validate",
        outcome="ready",
        checkpoint_reference=f"work/v2-journal/{post_id}.json",
        applied=bool(write["media_readback"]),
    )
    with connect_media_registry(tmp_path) as db:
        usages = db.execute(
            "SELECT wordpress_media_id, role, status FROM media_usage "
            "WHERE post_id=? ORDER BY wordpress_media_id",
            (post_id,),
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in usages] == [
        (115288, "featured", "applied"),
        (115325, "inline", "applied"),
        (115326, "inline", "applied"),
    ]


def test_writer_allows_only_listicle_final_repeat_of_featured(tmp_path):
    class Client:
        def __init__(self):
            self.post = {"id": 133, "status": "pending", "content": {"raw": "old"}, "featured_media": 0, "meta": {}}

        def get_post(self, _post_id):
            return self.post

        def update_post(self, _post_id, update):
            self.post.update(update)

    featured = FeaturedProgress(
        FeaturedStatus.VALID, 100, "https://example.test/featured.webp",
        "f" * 64, "f" * 16, "visual:" + "f" * 24, {"decision": "INITIAL"},
    )
    accepted = tuple(
        _verified_inline(media_id, f"https://example.test/{media_id}.webp", index * 3)
        for index, media_id in enumerate((201, 202, 203, 204))
    )
    repeated = InlineMedia(
        205, featured.media_url, 12,
        sha256=featured.sha256, phash=featured.phash, visual_group_id=featured.visual_group_id,
        visual_verification={"decision": "SAME_IMAGE", "duplicate_of": "100"},
    )
    media = MediaProgress(required=4, inline=(*accepted, repeated), featured=featured)
    content = (
        "<h2>1. Primeiro</h2>" + f'<img src="{accepted[0].media_url}">'
        + "<h2>2. Segundo</h2>" + f'<img src="{accepted[1].media_url}">'
        + f'<img src="{accepted[2].media_url}"><img src="{accepted[3].media_url}">'
        + f'<img src="{featured.media_url}">'
    )
    backup = tmp_path / "backups" / "133"
    backup.mkdir(parents=True)
    candidate = {
        "title": "Top 2 jogos", "content": content,
        "editorial": {"cleaned_html": content, "seo": {}},
        "seo": {"title": "Top 2 jogos", "meta_description": "Lista editorial com os quatro jogos distintos apresentados e análise suficiente para o leitor compreender cada recomendação.", "focus_keyword": "jogos"},
        "featured_media": featured.media_id, "media": media.to_dict(),
    }
    (backup / "editorial.candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    proposed = WorkState(state=LifecycleState.READY, phase=Phase.VALIDATE, relevance_approved=True, media=media)

    result = WordPressWriterV2(Client(), tmp_path).commit(
        133, {"post_id": 133}, proposed, Outcome.ready(),
    )

    assert result["media_readback"] is True
    assert len(_embedded_media_urls(content)) == 5


def test_writer_rejects_listicle_duplicate_not_at_final_position(tmp_path):
    class Client:
        def get_post(self, _post_id):
            return {"id": 134, "status": "pending", "content": {"raw": "old"}, "featured_media": 0, "meta": {}}

    featured = FeaturedProgress(
        FeaturedStatus.VALID, 100, "https://example.test/featured.webp",
        "f" * 64, "f" * 16, "visual:" + "f" * 24, {"decision": "INITIAL"},
    )
    first = _verified_inline(201, "https://example.test/201.webp", 0)
    repeated = InlineMedia(
        205, featured.media_url, 3,
        sha256=featured.sha256, phash=featured.phash, visual_group_id=featured.visual_group_id,
        visual_verification={"decision": "SAME_IMAGE", "duplicate_of": "100"},
    )
    media = MediaProgress(required=1, inline=(first, repeated), featured=featured)
    content = (
        "<h2>1. Primeiro</h2>" + f'<img src="{featured.media_url}">'
        + "<h2>2. Segundo</h2>" + f'<img src="{first.media_url}">'
    )
    backup = tmp_path / "backups" / "134"
    backup.mkdir(parents=True)
    (backup / "editorial.candidate.json").write_text(json.dumps({
        "title": "Top 2 jogos", "content": content, "editorial": {"cleaned_html": content, "seo": {}},
        "seo": {"title": "Top 2 jogos", "meta_description": "Lista editorial com os quatro jogos distintos apresentados e análise suficiente para o leitor compreender cada recomendação.", "focus_keyword": "jogos"},
        "featured_media": featured.media_id, "media": media.to_dict(),
    }), encoding="utf-8")
    proposed = WorkState(state=LifecycleState.READY, phase=Phase.VALIDATE, relevance_approved=True, media=media)

    with pytest.raises(RuntimeError, match="ineligible visual media"):
        WordPressWriterV2(Client(), tmp_path).commit(134, {"post_id": 134}, proposed, Outcome.ready())


def test_readback_identity_uses_img_elements_not_plain_text_urls():
    url = "https://example.test/a.webp"
    assert _embedded_media_urls(f"<p>{url}</p>") == []
    assert _embedded_media_urls(
        f'<figure><img src="https://EXAMPLE.test/a.webp#frame" /></figure>'
    ) == ["https://example.test/a.webp"]


def test_writer_moves_human_required_to_wordpress_awaiting_human(tmp_path):
    class Client:
        def __init__(self):
            self.post = {
                "id": 141,
                "status": "pending",
                "content": {"raw": "<p>texto anterior</p>"},
                "featured_media": 0,
                "meta": {},
            }
            self.status_moves = []

        def get_post(self, _post_id):
            return self.post

        def update_post(self, _post_id, update):
            self.post.update(update)

        def move_to_status(self, post_id, status):
            self.status_moves.append((post_id, status))
            self.post["status"] = status
            return self.post

    client = Client()
    state = WorkState(
        state=LifecycleState.HUMAN_REQUIRED,
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        relevance_approved=True,
        media=MediaProgress(required=4),
    )
    result = WordPressWriterV2(
        client, tmp_path
    ).commit(141, {"post_id": 141}, state, Outcome.human_required(Phase.MEDIA, BlockerCode.INLINE_MISSING))

    assert result["readback"] is True
    assert client.status_moves == [(141, "awaiting_human")]
    assert client.post["status"] == "awaiting_human"


def test_writer_does_not_apply_candidate_content_while_partial(tmp_path):
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
        json.dumps({
            "title": "Título editorial ainda incompleto",
            "content": f'<p>texto</p><img src="{inline_url}" />',
        }),
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
    assert client.post["content"]["raw"] == "<p>texto</p>"
    assert len(client.updates) == 1
    assert "content" not in client.updates[0]
    assert "title" not in client.updates[0]
    journal = json.loads((tmp_path / "work/v2-journal/14.json").read_text())
    assert journal["media_state"] == "accepted_working"
    assert journal["media_readback_status"] == "not_attempted"
    assert journal["media_ids"] == [14]
    assert journal["media_records"][0]["destination"] == "pending_apply"

    second = WordPressWriterV2(client, tmp_path).commit(14, {"post_id": 14}, state, outcome)
    assert second["readback"] is True
    assert len(client.updates) == 1
    assert client.post["content"]["raw"] == "<p>texto</p>"


def test_writer_ignores_candidate_from_previous_run_after_compose_failure(tmp_path):
    inline_url = "https://example.test/stale.webp"

    class Client:
        def __init__(self):
            self.post = {
                "id": 15,
                "status": "pending",
                "content": {"raw": "<p>conteudo anterior</p>"},
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
    backup = tmp_path / "backups" / "15"
    backup.mkdir(parents=True)
    (backup / "editorial.candidate.json").write_text(
        json.dumps({
            "_v2_run_id": "old-run",
            "content": f'<p>conteudo obsoleto</p><img src="{inline_url}" />',
        }),
        encoding="utf-8",
    )
    state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.COMPOSE,
        blocker=BlockerCode.MANIFEST_INVALID,
        relevance_approved=True,
        media=MediaProgress(required=1, inline=(InlineMedia(15, inline_url, 0),)),
    )
    result = WordPressWriterV2(client, tmp_path).commit(
        15,
        {
            "post_id": 15,
            "v2_run_id": "new-run",
            "candidate_must_be_fresh": True,
        },
        state,
        Outcome(OutcomeType.RETRY, Phase.COMPOSE, BlockerCode.MANIFEST_INVALID),
    )
    assert result["readback"] is True
    assert client.post["content"]["raw"] == "<p>conteudo anterior</p>"
    assert "content" not in client.updates[-1]
    journal = json.loads((tmp_path / "work/v2-journal/15.json").read_text())
    assert journal["candidate_fresh"] is False


def test_writer_does_not_require_partial_candidate_readback(tmp_path):
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

    result = WordPressWriterV2(Client(), tmp_path).commit(
        15,
        {"post_id": 15},
        state,
        Outcome.retry(Phase.MEDIA, BlockerCode.INLINE_MISSING),
    )
    assert result["readback"] is True


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
