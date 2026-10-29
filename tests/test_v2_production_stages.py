import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from unicornio_editor.pipeline_v2.errors import StageError
from unicornio_editor.pipeline_v2.model import BlockerCode, FeaturedProgress, FeaturedStatus, InlineMedia, LifecycleState, MediaProgress, MediaSearchProgress, Outcome, OutcomeType, Phase, RetryInfo, WorkState
from unicornio_editor.pipeline_v2.production_stages import (
    ProductionComposeStage,
    ProductionEditorialStage,
    ProductionMediaStage,
    ProductionValidateStage,
    _checklist_failures,
    _ensure_prepared_media_structure,
    _prepare_editorial_media_structure,
    _repair_focus_keyword_draft,
)
from unicornio_editor.pipeline_v2.production import ProductionContextLoader, recover_editorial, editorial_has_substantive_text
from unicornio_editor.pipeline_v2.runtime import (
    ProductionMediaResolver,
    WordPressWriterV2,
    _embedded_media_urls,
    _v2_inline_media_ids_to_invalidate,
)
from unicornio_editor.media.library_index import register as register_library_media
from unicornio_editor.media.registry import (
    connect as connect_media_registry,
    invalidate_v2_media,
    record_v2_observation,
)
from unicornio_editor.workflow import MediaFunnelInvariantError, _resolve_seo_from_post, resolve_editorial_defaults
from unicornio_editor.editorial_schema import validate_editorial


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


def _long_single_sentence_body(paragraphs=10):
    sentence = " ".join(["Videogame"] + [f"detalhe{index}" for index in range(70)] + ["confirmado."])
    return "".join(f"<p>{sentence}</p>" for _ in range(paragraphs))


def _substantive_body(paragraphs=6):
    sentence = (
        "A equipe confirmou detalhes importantes sobre o videogame e explicou os próximos "
        "passos nesta atualização recente para os jogadores interessados."
    )
    return "".join(f"<p>{sentence}</p>" for _ in range(paragraphs))


def test_writer_commit_emits_write_intent_and_readback_contract(tmp_path):
    post_id = 99181
    state = WorkState(
        state=LifecycleState.HUMAN_REQUIRED,
        phase=Phase.MEDIA,
        blocker=BlockerCode.FEATURED_MISSING,
        relevance_approved=True,
        media=MediaProgress(required=2),
    )

    class Client:
        def __init__(self):
            self.post = {
                "id": post_id,
                "status": "pending",
                "title": {"raw": "Teste de journal"},
                "content": {"raw": "<p>Conteúdo existente.</p>"},
                "featured_media": 0,
                "meta": {},
            }
            self.update_calls = 0

        def get_post(self, _post_id):
            return json.loads(json.dumps(self.post))

        def update_post(self, _post_id, payload):
            self.update_calls += 1
            self.post.update({key: value for key, value in payload.items() if key != "meta"})
            self.post.setdefault("meta", {}).update(payload.get("meta") or {})

        def move_to_status(self, _post_id, status):
            self.post["status"] = status

    client = Client()
    result = WordPressWriterV2(client, tmp_path).commit(
        post_id,
        {
            "post_id": post_id,
            "v2_run_id": "writer-contract-run",
            "v2_cycle_id": "writer-contract-cycle",
            "v2_cycle_round": 1,
            "v2_authorization_id": "writer-contract-auth",
        },
        state,
        Outcome.human_required(Phase.MEDIA, BlockerCode.FEATURED_MISSING, "featured missing"),
    )

    journal = json.loads(
        (tmp_path / "work" / "v2-journal" / f"{post_id}.json").read_text(encoding="utf-8")
    )
    assert result["wordpress_changed"] is True
    assert client.update_calls == 1
    assert journal["status"] == "committed"
    assert journal["readback"] is True
    assert journal["writer_started"] is True
    assert journal["writer_status"] == "readback_confirmed"
    assert journal["writer_outcome"] == "changed"


def _human_required_state_for_writer():
    return WorkState(
        state=LifecycleState.HUMAN_REQUIRED,
        phase=Phase.MEDIA,
        blocker=BlockerCode.FEATURED_MISSING,
        relevance_approved=True,
        media=MediaProgress(required=0),
    )


class _StatusTransitionClient:
    def __init__(self, post_id, status="pending", *, move_error=None, interrupt_after_move=False, divergent=False):
        self.post = {
            "id": post_id,
            "status": status,
            "title": {"raw": "Teste"},
            "content": {"raw": "<p>Conteúdo existente.</p>"},
            "featured_media": 0,
            "meta": {},
        }
        self.move_calls = 0
        self.update_calls = 0
        self.get_calls = 0
        self.move_error = move_error
        self.interrupt_after_move = interrupt_after_move
        self.divergent = divergent

    def get_post(self, _post_id):
        self.get_calls += 1
        if self.interrupt_after_move and self.get_calls == 2:
            raise ConnectionError("readback interrupted")
        return json.loads(json.dumps(self.post))

    def update_post(self, _post_id, payload):
        self.update_calls += 1
        self.post.update({key: value for key, value in payload.items() if key != "meta"})
        self.post.setdefault("meta", {}).update(payload.get("meta") or {})

    def move_to_status(self, _post_id, status):
        self.move_calls += 1
        if self.move_error:
            raise self.move_error
        if not self.divergent:
            self.post["status"] = status


def _commit_human_required_status_transition(tmp_path, client, *, existing_work_state=None):
    post_id = client.post["id"]
    state = _human_required_state_for_writer()
    state_json = json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":"))
    client.post["meta"]["_hermes_work_state"] = (
        state_json if existing_work_state is None else existing_work_state
    )
    return WordPressWriterV2(client, tmp_path).commit(
        post_id,
        {
            "post_id": post_id,
            "v2_run_id": f"status-run-{post_id}",
            "v2_cycle_id": "status-cycle",
            "v2_cycle_round": 1,
            "v2_authorization_id": "status-auth",
        },
        state,
        Outcome.human_required(Phase.MEDIA, BlockerCode.FEATURED_MISSING, "featured missing"),
    )


def _read_writer_journal(root, post_id):
    return json.loads((root / "work" / "v2-journal" / f"{post_id}.json").read_text(encoding="utf-8"))


def test_human_required_pending_moves_status_and_records_writer_effect(tmp_path):
    client = _StatusTransitionClient(99201, status="pending")
    result = _commit_human_required_status_transition(tmp_path, client)
    journal = _read_writer_journal(tmp_path, 99201)
    assert client.update_calls == 0
    assert client.move_calls == 1
    assert result["wordpress_changed"] is True
    assert result["writer_started"] is True
    assert result["writer_outcome"] == "changed"
    assert journal["writer_started"] is True
    assert journal["writer_status"] == "readback_confirmed"
    assert journal["writer_outcome"] == "changed"


def test_human_required_already_awaiting_human_does_not_write(tmp_path):
    client = _StatusTransitionClient(99202, status="awaiting_human")
    result = _commit_human_required_status_transition(tmp_path, client)
    journal = _read_writer_journal(tmp_path, 99202)
    assert client.update_calls == 0
    assert client.move_calls == 0
    assert result["wordpress_changed"] is False
    assert result["writer_started"] is False
    assert result["writer_outcome"] == "no_change"
    assert journal["writer_started"] is False
    assert journal["writer_status"] == "not_started"
    assert journal["writer_outcome"] == "no_change"


def test_human_required_metadata_update_preserves_confirmed_writer_evidence(tmp_path):
    client = _StatusTransitionClient(99206, status="awaiting_human")
    result = _commit_human_required_status_transition(
        tmp_path, client, existing_work_state="{}"
    )
    journal = _read_writer_journal(tmp_path, 99206)

    assert client.update_calls == 1
    assert client.move_calls == 0
    assert client.post["meta"]["_hermes_work_state"] == json.dumps(
        _human_required_state_for_writer().to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert result["wordpress_changed"] is True
    assert result["writer_started"] is True
    assert result["writer_status"] == "readback_confirmed"
    assert result["writer_outcome"] == "changed"
    assert journal["status"] == "committed"
    assert journal["readback"] is True
    assert journal["writer_started"] is True
    assert journal["writer_status"] == "readback_confirmed"
    assert journal["writer_outcome"] == "changed"


@pytest.mark.parametrize(
    "client_kwargs, expected_status, expected_writer_status",
    [
        ({"move_error": RuntimeError("status POST failed")}, "writer_failed", "possibly_started"),
        ({"interrupt_after_move": True}, "readback_failed", "submitted"),
        ({"divergent": True}, "readback_failed", "submitted"),
    ],
)
def test_human_required_status_transition_failures_remain_conservative(
    tmp_path, client_kwargs, expected_status, expected_writer_status
):
    client = _StatusTransitionClient(99203, status="pending", **client_kwargs)
    with pytest.raises(Exception):
        _commit_human_required_status_transition(tmp_path, client)
    journal = _read_writer_journal(tmp_path, 99203)
    assert journal["status"] == expected_status
    assert journal["status"] != "committed"
    assert journal["writer_started"] is True
    assert journal["writer_status"] == expected_writer_status
    assert journal["writer_outcome"] in {"failed", "uncertain"}



def test_media_registry_evidence_uses_writer_result_for_status_transition(tmp_path):
    client = _StatusTransitionClient(99204, status="pending")
    result = _commit_human_required_status_transition(tmp_path, client)
    state = _human_required_state_for_writer()
    record_v2_observation(
        tmp_path,
        post_id=99204,
        run_id="status-run-99204",
        state=state,
        phase=state.phase.value,
        outcome="human_required",
        cycle_id="status-cycle",
        cycle_round=1,
        authorization_id="status-auth",
        operation_status="completed",
        write_started=bool(result["writer_started"]),
        readback_confirmed=bool(result["readback"]),
    )
    with connect_media_registry(tmp_path) as db:
        row = db.execute(
            "SELECT operation_status, write_started, readback_confirmed FROM media_operations WHERE run_id=?",
            ("status-run-99204",),
        ).fetchone()
    assert tuple(row) == ("completed", 1, 1)
    assert result["writer_started"] is True
    assert result["writer_status"] == "readback_confirmed"
    assert result["writer_outcome"] == "changed"



def test_media_registry_evidence_preserves_no_change_for_existing_status(tmp_path):
    client = _StatusTransitionClient(99205, status="awaiting_human")
    result = _commit_human_required_status_transition(tmp_path, client)
    state = _human_required_state_for_writer()
    record_v2_observation(
        tmp_path,
        post_id=99205,
        run_id="status-run-99205",
        state=state,
        phase=state.phase.value,
        outcome="human_required",
        operation_status="completed",
        write_started=bool(result["writer_started"]),
        readback_confirmed=bool(result["readback"]),
    )
    with connect_media_registry(tmp_path) as db:
        row = db.execute(
            "SELECT operation_status, write_started, readback_confirmed FROM media_operations WHERE run_id=?",
            ("status-run-99205",),
        ).fetchone()
    assert tuple(row) == ("completed", 0, 1)
    assert result["writer_started"] is False
    assert result["writer_status"] == "not_started"
    assert result["writer_outcome"] == "no_change"



def test_media_stage_reuses_existing_and_writes_manifest(tmp_path):
    existing = InlineMedia(10, "https://example.test/a.webp", 0, "A", "Crédito da imagem: A")
    stage = ProductionMediaStage(object(), Config(), tmp_path, resolver=lambda *_args: MediaProgress(
        required=2, inline=(existing,), featured=FeaturedProgress(FeaturedStatus.MISSING)
    ))
    result = stage(
        {"post_id": 7, "title": "Notícia sobre videogame"},
        SimpleNamespace(media=MediaProgress(required=2, inline=(existing,))),
        {"cleaned_html": _substantive_body(), "site_relevance": {"decision": "process"}},
    )
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




def test_context_loader_recovers_wordpress_content_when_draft_is_empty(tmp_path):
    body = _long_single_sentence_body(4)
    post = {"id": 115233, "title": "Título", "content": body, "meta": {}}
    directory = tmp_path / "backups" / "115233"
    directory.mkdir(parents=True)
    (directory / "editorial.draft.json").write_text(json.dumps({"cleaned_html": ""}), encoding="utf-8")

    context = ProductionContextLoader(object(), tmp_path).load(post)

    assert context["editorial"] is not None
    assert context["editorial"]["cleaned_html"] == body
    assert context["editorial_recovery"]["source"] == "wordpress_get"
    assert editorial_has_substantive_text(context["editorial"])


def test_recover_editorial_accepts_verified_prepared_source_without_wp_content(tmp_path):
    body = _long_single_sentence_body(4)
    directory = tmp_path / "backups" / "115234"
    directory.mkdir(parents=True)
    (directory / "prepared.json").write_text(json.dumps({"cleaned_html": body}), encoding="utf-8")

    editorial, recovery = recover_editorial({"id": 115234, "title": "Título"}, directory)

    assert editorial["cleaned_html"] == body
    assert recovery["source"] == "prepared.json"


def test_recover_editorial_prefers_fresh_valid_draft_over_wordpress(tmp_path):
    draft_body = _long_single_sentence_body(4) + "<p>Rascunho editorial preservado.</p>"
    wp_body = _long_single_sentence_body(4) + "<p>Conteúdo já aplicado.</p>"
    directory = tmp_path / "backups" / "115236"
    directory.mkdir(parents=True)
    (directory / "editorial.draft.json").write_text(json.dumps({"cleaned_html": draft_body}), encoding="utf-8")

    editorial, recovery = recover_editorial(
        {"id": 115236, "title": "Título", "content": wp_body, "modified_gmt": "2000-01-01T00:00:00+00:00"},
        directory,
    )

    assert editorial["cleaned_html"] == draft_body
    assert recovery["source"] == "editorial.draft.json"


def test_recover_editorial_projects_legacy_operational_envelope(tmp_path):
    body = "<p>" + ("texto editorial " * 40) + "</p>"
    directory = tmp_path / "backups" / "115239"
    directory.mkdir(parents=True)
    (directory / "editorial.draft.json").write_text(json.dumps({
        "cleaned_html": body,
        "title": "Título válido",
        "site_relevance": {"decision": "process", "confidence": 0.95, "reason": "ok", "matched_topics": ["games"]},
        "media_plan": [], "needs_trailer": False, "trailer_url": None, "game_name": "Jogo",
        "seo": {"title": "Título válido", "meta_description": "descrição válida sobre o artigo editorial e os detalhes confirmados para os leitores interessados no tema apresentado hoje.", "focus_keyword": "jogo"},
        "backup": "snapshot.json", "original_link": "https://source.test/article",
        "post_id": 115239, "status": "pending", "wordpress_changed": False,
    }), encoding="utf-8")

    editorial, recovery = recover_editorial({"id": 115239, "title": "Título"}, directory)

    assert editorial is not None
    assert editorial["cleaned_html"] == body
    assert "backup" not in editorial
    assert "original_link" not in editorial
    assert "post_id" not in editorial
    assert recovery["projected_fields"] == ["backup", "original_link", "post_id", "status", "wordpress_changed"]
    validate_editorial(editorial)


def test_recover_editorial_preserves_verified_metadata_with_body_only_draft(tmp_path):
    body = "<p>" + ("texto editorial " * 40) + "</p>"
    directory = tmp_path / "backups" / "115237"
    directory.mkdir(parents=True)
    (directory / "editorial.draft.json").write_text(json.dumps({"cleaned_html": body}), encoding="utf-8")
    (directory / "prepared.json").write_text(json.dumps({"cleaned_html": body + "\n<!-- wc: 40 -->"}), encoding="utf-8")
    work = tmp_path / "work" / "v2-editorial"
    work.mkdir(parents=True)
    (work / "115237.json").write_text(json.dumps({"results": [{"post_id": 115237, "editorial": {
        "title": "Título validado", "site_relevance": {"decision": "process", "confidence": 0.95},
        "media_plan": [], "needs_trailer": False, "trailer_url": None, "game_name": "Jogo",
        "seo": {"title": "Título validado", "meta_description": "descrição", "focus_keyword": "Jogo"},
    }}]}), encoding="utf-8")
    candidate = {"_v2_run_id": "run-verified", "content": body}
    (directory / "editorial.candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    journal = tmp_path / "work" / "v2-journal"
    journal.mkdir(parents=True)
    (journal / "115237.json").write_text(json.dumps({
        "post_id": 115237, "candidate_run_id": "run-verified",
        "candidate_hash": hashlib.sha256(body.encode()).hexdigest(),
    }), encoding="utf-8")

    editorial, recovery = recover_editorial(
        {"id": 115237, "title": "Título", "content": "<p>WordPress posterior</p>", "modified_gmt": "2000-01-01T00:00:00+00:00"},
        directory,
    )

    assert recovery["source"] == "editorial.draft.json"
    assert editorial["cleaned_html"] == body
    assert editorial["site_relevance"]["decision"] == "process"
    assert editorial["needs_trailer"] is False




def test_recover_editorial_rejects_metadata_from_stale_generation(tmp_path):
    body = "<p>" + ("texto editorial " * 40) + "</p>"
    directory = tmp_path / "backups" / "115238"
    directory.mkdir(parents=True)
    (directory / "editorial.draft.json").write_text(json.dumps({"cleaned_html": body}), encoding="utf-8")
    (directory / "prepared.json").write_text(json.dumps({"cleaned_html": body}), encoding="utf-8")
    (directory / "editorial.candidate.json").write_text(json.dumps({
        "_v2_run_id": "run-old", "content": body,
    }), encoding="utf-8")
    work = tmp_path / "work" / "v2-editorial"
    work.mkdir(parents=True)
    (work / "115238.json").write_text(json.dumps({"results": [{"post_id": 115238, "editorial": {
        "title": "Metadado antigo", "site_relevance": {"decision": "process"},
    }}]}), encoding="utf-8")
    journal = tmp_path / "work" / "v2-journal"
    journal.mkdir(parents=True)
    (journal / "115238.json").write_text(json.dumps({
        "post_id": 115238, "candidate_run_id": "run-current",
        "candidate_hash": hashlib.sha256(body.encode()).hexdigest(),
    }), encoding="utf-8")

    editorial, _recovery = recover_editorial(
        {"id": 115238, "title": "Título", "content": "", "modified_gmt": "2000-01-01T00:00:00+00:00"},
        directory,
    )

    assert editorial["cleaned_html"] == body
    assert "site_relevance" not in editorial
    assert "title" not in editorial


def test_recover_editorial_rejects_stale_snapshot_after_wp_change(tmp_path):
    body = _long_single_sentence_body(4)
    directory = tmp_path / "backups" / "115235"
    directory.mkdir(parents=True)
    backup = directory / "snapshot.json"
    backup.write_text(json.dumps({"modified_gmt": "old"}), encoding="utf-8")
    (directory / "prepared.json").write_text(
        json.dumps({"cleaned_html": body, "backup": "snapshot.json"}), encoding="utf-8"
    )

    editorial, recovery = recover_editorial(
        {"id": 115235, "title": "Título", "content": "", "modified_gmt": "new"},
        directory,
    )

    assert editorial is None
    assert recovery["reason"] == "no_verifiable_editorial_source"


def test_markup_only_and_null_editorial_are_not_recoverable():
    assert not editorial_has_substantive_text({"cleaned_html": "<p><br /></p><img src=\"x\">"})
    assert not editorial_has_substantive_text({"cleaned_html": None})




def test_editorial_defaults_fill_optional_trailer_contract():
    resolved = resolve_editorial_defaults(
        {"site_relevance": {"decision": "process", "confidence": 0.95, "reason": "ok", "matched_topics": ["games"]}, "media_plan": [], "game_name": None},
        {"title": {"raw": "Notícia sobre jogos"}, "content": {"raw": "<p>Notícia sobre jogos confirmada. " + ("texto " * 80) + "</p>"}, "meta": {}},
    )
    assert resolved["needs_trailer"] is False
    assert resolved["trailer_url"] is None


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
        stage(
            {"post_id": 7, "title": "Notícia sobre videogame"},
            SimpleNamespace(media=MediaProgress(required=1)),
            {"cleaned_html": _substantive_body(), "site_relevance": {"decision": "process"}},
        )
    assert error.value.blocker is BlockerCode.INTERNAL_ERROR
    assert error.value.phase is Phase.MEDIA


def test_media_stage_logs_contract_error_with_run_identity_and_redacted_traceback(tmp_path):
    stage = ProductionMediaStage(
        object(), Config(), tmp_path,
        resolver=lambda *_args: (_ for _ in ()).throw(TypeError("token=must-not-leak")),
    )
    with pytest.raises(StageError) as error:
        stage(
            {"post_id": 115152, "title": "Notícia sobre videogame", "v2_run_id": "run-contract"},
            SimpleNamespace(media=MediaProgress(required=1)),
            {"cleaned_html": _substantive_body(), "site_relevance": {"decision": "process"}},
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
    body = "".join(
        f"<p>Vazamentos sobre GTA 6 revelam novidades. Este parágrafo explica os detalhes conhecidos e o contexto para os leitores atentos. {index}.</p>"
        for index in range(5)
    )
    draft = {
        "cleaned_html": body,
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
            "cleaned_html": "".join(
                f"<p>Vazamentos sobre GTA 6 revelam novidades. O anúncio oferece contexto e detalhes úteis aos leitores atentos, edição {index}.</p>"
                for index in range(5)
            ),
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


def test_editorial_stage_prepares_body_before_media_and_places_final_image_before_footer(tmp_path, monkeypatch):
    post_id = 115777
    title = "Atualização semanal de videogame"
    sentence = (
        "A equipe confirmou novos detalhes importantes sobre videogame, lançamento, jogadores e recursos adicionais nesta semana. "
    )
    paragraphs = [f"<p>{sentence * 6}</p>" for _ in range(8)]
    paragraphs.append(f"<p>{sentence * 6}ENCERRAMENTO EDITORIAL ÚNICO.</p>")
    raw_body = "".join(paragraphs[:3]) + "<p><br /></p><p> &nbsp; </p>" + "".join(paragraphs[3:])
    legacy_footer = (
        '<hr /><h3>Confira mais novidades em nosso Portal de Notícias!</h3><hr />'
        '<em>Fonte: <a href="https://source.example/article">Source</a></em>'
    )
    raw_body_with_footer = raw_body + legacy_footer
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"posts": [{"post_id": post_id, "title": title}]}), encoding="utf-8")
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.prepare_batch",
        lambda *_args: {"prepared": 1, "editorial_input": str(input_path)},
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "videogame"}],
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.generate_editorial_batch",
        lambda *_args, **_kwargs: {"output": "generated.json"},
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.load_editorial_batch",
        lambda *_args: {"items": [{"post_id": post_id, "editorial": {
            "title": title,
            "cleaned_html": raw_body_with_footer,
            "seo": {"title": title, "focus_keyword": "videogame"},
            "site_relevance": {"decision": "process"},
        }}]},
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.resolve_editorial_defaults",
        lambda value, _post: value,
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.validate_editorial",
        lambda value, **_kwargs: value,
    )

    state = WorkState(phase=Phase.EDITORIAL)
    editorial = ProductionEditorialStage(object(), SimpleNamespace(
        editorial_api_key="x", editorial_base_url="x", editorial_model="x",
        http_timeout=1, min_relevance_confidence=0.8,
    ), tmp_path)(
        {"post_id": post_id, "title": title, "post": {"id": post_id, "title": {"raw": title}}},
        state,
    )

    audit = json.loads((tmp_path / "backups" / str(post_id) / "editorial.structure.json").read_text())
    assert audit["status"] == "prepared"
    assert audit["layout"] == "normal_article"
    assert audit["empty_paragraphs_removed"] == 2
    assert audit["required_inline"] == 4
    assert audit["paragraphs_after"] == 11
    assert "Confira mais novidades" not in editorial["cleaned_html"]
    assert "ENCERRAMENTO EDITORIAL ÚNICO." in editorial["cleaned_html"]

    resolved = {}

    def resolver(_context, _state, prepared_editorial, _previous):
        resolved["paragraphs"] = prepared_editorial["cleaned_html"].count("<p>")
        resolved["body"] = prepared_editorial["cleaned_html"]
        return MediaProgress(required=4)

    ProductionMediaStage(object(), Config(), tmp_path, resolver=resolver)(
        {"post_id": post_id}, state, editorial,
    )
    assert resolved["paragraphs"] == 11

    media = MediaProgress(
        required=4,
        inline=tuple(
            _verified_inline(500 + index, f"https://example.test/layout-{index}.webp", index)
            for index in range(4)
        ),
    )
    candidate = ProductionComposeStage(Config(), tmp_path)(
        {"post_id": post_id, "title": title, "original_link": "https://source.example/article"},
        editorial,
        media,
    )
    final_placement = candidate["media_placements"][-1]
    assert final_placement["placement"] == "after_editorial_body"
    final_image_position = candidate["content"].rfind("[caption id=\"\" align=\"aligncenter\"")
    end_marker_position = candidate["content"].index("ENCERRAMENTO EDITORIAL ÚNICO.")
    cta_position = candidate["content"].index("Confira mais novidades")
    assert end_marker_position < final_image_position < cta_position


def test_capacity_limited_normal_article_gets_audited_layout_reduction(tmp_path, monkeypatch):
    post_id = 115778
    title = "Atualização semanal de videogame"
    sentence = " ".join(
        ["Videogame"] + [f"detalhe{index}" for index in range(70)] + ["confirmado."]
    )
    one_sentence_paragraphs = "".join(f"<p>{sentence}</p>" for _ in range(9))
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"posts": [{"post_id": post_id, "title": title}]}), encoding="utf-8")
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.prepare_batch",
        lambda *_args: {"prepared": 1, "editorial_input": str(input_path)},
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "videogame"}],
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.load_editorial_batch",
        lambda *_args: {"items": [{"post_id": post_id, "editorial": {
            "title": title,
            "cleaned_html": one_sentence_paragraphs,
            "seo": {"title": title, "focus_keyword": "videogame"},
            "site_relevance": {"decision": "process"},
        }}]},
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.resolve_editorial_defaults",
        lambda value, _post: value,
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.validate_editorial",
        lambda value, **_kwargs: value,
    )
    stage = ProductionEditorialStage(object(), SimpleNamespace(
        editorial_api_key="x", editorial_base_url="x", editorial_model="x",
        http_timeout=1, min_relevance_confidence=0.8,
    ), tmp_path)
    context = {"post_id": post_id, "title": title, "post": {"id": post_id, "title": {"raw": title}}}
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.generate_editorial_batch",
        lambda *_args, **_kwargs: {"output": "generated.json"},
    )
    prepared = stage(context, WorkState(phase=Phase.EDITORIAL))
    audit_path = tmp_path / "backups" / str(post_id) / "editorial.structure.json"
    audit = json.loads(audit_path.read_text())
    assert audit["status"] == "prepared"
    assert audit["target_ideal"] == 4
    assert audit["capacity_verified"] == 3
    assert audit["target_effective"] == 3
    assert audit["reduction_reason"] == "layout_limited"
    assert prepared["cleaned_html"] == one_sentence_paragraphs


def test_media_stage_uses_audited_effective_target_before_provider_resolution(tmp_path, monkeypatch):
    body = _long_single_sentence_body(10)
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "videogame"}],
    )
    observed = {}

    def resolver(_context, state, _editorial, previous):
        observed["state_required"] = state.media.required
        observed["previous_required"] = previous.required
        observed["policy"] = previous.layout_policy
        return MediaProgress(required=previous.required)

    state = WorkState(phase=Phase.MEDIA, media=MediaProgress(required=4))
    result = ProductionMediaStage(object(), Config(), tmp_path, resolver=resolver)(
        {"post_id": 115779, "title": "Atualização semanal de videogame"},
        state,
        {"cleaned_html": body, "site_relevance": {"decision": "process"}},
    )
    assert observed["state_required"] == 3
    assert observed["previous_required"] == 3
    assert observed["policy"]["reduction_reason"] == "layout_limited"
    assert result.required == 3
    assert result.layout_policy == observed["policy"]


def test_empty_structure_error_does_not_replace_valid_draft(tmp_path):
    post_id = 115792
    directory = tmp_path / "backups" / str(post_id)
    directory.mkdir(parents=True)
    valid = {"cleaned_html": _long_single_sentence_body(4), "seo": {"title": "Rascunho válido"}}
    draft_path = directory / "editorial.draft.json"
    draft_path.write_text(json.dumps(valid), encoding="utf-8")

    with pytest.raises(StageError) as raised:
        _ensure_prepared_media_structure(
            tmp_path,
            post_id,
            {"cleaned_html": ""},
            WorkState(phase=Phase.EDITORIAL),
            title="Título sem conteúdo",
        )

    assert raised.value.blocker == BlockerCode.STRUCTURE
    assert json.loads(draft_path.read_text(encoding="utf-8")) == valid


def test_valid_structure_error_persists_verifiable_editorial(tmp_path):
    post_id = 115793
    body = "<p>" + ("palavra " * 80) + "</p>"
    with pytest.raises(StageError):
        _ensure_prepared_media_structure(
            tmp_path,
            post_id,
            {"cleaned_html": body},
            WorkState(phase=Phase.EDITORIAL),
            title="Título válido",
        )
    persisted = json.loads(
        (tmp_path / "backups" / str(post_id) / "editorial.draft.json").read_text(encoding="utf-8")
    )
    assert persisted["cleaned_html"] == body


def test_compose_keeps_quota_body_separate_from_composed_media_html(tmp_path, monkeypatch):
    body = _long_single_sentence_body(10)
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "videogame"}],
    )
    prepared, audit, error = _prepare_editorial_media_structure(
        {"cleaned_html": body, "seo": {"title": "Notícia sobre videogame", "focus_keyword": "videogame"}},
        WorkState(phase=Phase.EDITORIAL),
        title="Notícia sobre videogame",
    )
    assert error is None
    directory = tmp_path / "backups" / "115780"
    directory.mkdir(parents=True)
    (directory / "editorial.structure.json").write_text(json.dumps(audit), encoding="utf-8")
    (directory / "editorial.draft.json").write_text(json.dumps(prepared), encoding="utf-8")
    accepted = tuple(
        _verified_inline(index, f"https://media.example/{index}.webp", index - 1)
        for index in range(1, 4)
    )
    media = MediaProgress(
        required=3,
        inline=accepted,
        featured=FeaturedProgress(FeaturedStatus.MISSING),
        layout_policy=audit["media_layout_policy"],
    )
    candidate = ProductionComposeStage(Config(), tmp_path).compose_candidate(
        {"post_id": 115780, "title": "Notícia sobre videogame", "original_link": None},
        prepared,
        media,
    )
    assert candidate["editorial"]["cleaned_html"] == prepared["cleaned_html"]
    assert candidate["content"].count("<img") == 3
    assert candidate["media"]["inline"]["required"] == 3
    assert candidate["media"]["layout_policy"] == audit["media_layout_policy"]


def test_compose_revalidates_prepared_layout_when_featured_arrives_later(tmp_path, monkeypatch):
    post_id = 115784
    title = "Notícia sobre videogame"
    body = _long_single_sentence_body(11)
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "videogame"}],
    )
    prepared, audit, error = _prepare_editorial_media_structure(
        {"title": title, "cleaned_html": body, "seo": {}},
        WorkState(
            phase=Phase.EDITORIAL,
            media=MediaProgress(
                required=4,
                featured=FeaturedProgress(FeaturedStatus.MISSING),
            ),
        ),
        title=title,
    )
    assert error is None
    persisted_policy = audit["media_layout_policy"]
    assert "closing_featured_repeat" not in persisted_policy
    assert persisted_policy["placement_plan"][-1]["anchor"] == "after_editorial_body"

    directory = tmp_path / "backups" / str(post_id)
    directory.mkdir(parents=True)
    structure_path = directory / "editorial.structure.json"
    structure_path.write_text(json.dumps(audit), encoding="utf-8")
    (directory / "editorial.draft.json").write_text(
        json.dumps(prepared), encoding="utf-8",
    )
    original_structure = structure_path.read_bytes()

    featured = FeaturedProgress(
        FeaturedStatus.VALID,
        399,
        "https://media.example/featured-399.webp",
        "f" * 64,
        "f" * 16,
        "visual:featured-399",
        {"decision": "INITIAL"},
        "Arte de capa do videogame",
        "Crédito da imagem: Fonte oficial",
        1280,
        720,
    )
    inline = (
        _verified_inline(301, "https://media.example/inline-301.webp", 0),
        _verified_inline(302, "https://media.example/inline-302.webp", 1),
    )
    media = MediaProgress(
        required=4,
        inline=inline,
        featured=featured,
        search=MediaSearchProgress(
            completed=True,
            exhausted=True,
            completion_reason="EXHAUSTED_VERIFIED",
        ),
        waiver_applied=True,
        waiver_reason="search_exhausted",
        layout_policy=persisted_policy,
    )
    context = {"post_id": post_id, "title": title, "original_link": None}
    stage = ProductionComposeStage(Config(), tmp_path)
    candidate = stage.compose_candidate(context, dict(prepared), media)
    candidate_retry = stage.compose_candidate(context, dict(prepared), media)

    assert candidate["editorial"]["cleaned_html"] == prepared["cleaned_html"]
    assert candidate["media"]["layout_policy"] == persisted_policy
    assert "closing_featured_repeat" not in candidate["media"]
    assert candidate["content"].count("<img") == 2
    assert "featured-399.webp" not in candidate["content"]
    assert candidate_retry["content"] == candidate["content"]
    assert candidate_retry["media"]["layout_policy"] == persisted_policy
    assert structure_path.read_bytes() == original_structure


def test_layout_capacity_caps_candidate_and_preserves_excess_working_media(tmp_path, monkeypatch):
    post_id = 115790
    run_id = "run-layout-capacity-115790"
    title = "Notícia sobre videogame"
    body = _long_single_sentence_body(10)
    description = (
        "A atualização explica os detalhes confirmados sobre videogame, os impactos para os jogadores "
        "e o contexto necessário para acompanhar esta notícia."
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "videogame"}],
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.runtime.post_subjects",
        lambda **_kwargs: [{"subject": "videogame"}],
    )
    featured = FeaturedProgress(
        FeaturedStatus.VALID,
        399,
        "https://media.example/featured-399.webp",
        "f" * 64,
        "f" * 16,
        "visual:" + "f" * 24,
        {"decision": "INITIAL"},
        "Arte de capa do videogame",
        "Crédito da imagem: Fonte oficial",
        1280,
        720,
    )
    editorial = {
        "title": title,
        "cleaned_html": body,
        "seo": {
            "title": title,
            "meta_description": description,
            "focus_keyword": "videogame",
        },
        "site_relevance": {"decision": "process"},
    }
    prepared, audit, error = _prepare_editorial_media_structure(
        editorial,
        WorkState(phase=Phase.EDITORIAL, media=MediaProgress(required=4, featured=featured)),
        title=title,
    )
    assert error is None
    policy = audit["media_layout_policy"]
    assert audit["target_ideal"] == 4
    assert audit["target_effective"] == 3
    directory = tmp_path / "backups" / str(post_id)
    directory.mkdir(parents=True)
    (directory / "editorial.structure.json").write_text(json.dumps(audit), encoding="utf-8")
    (directory / "editorial.draft.json").write_text(json.dumps(prepared), encoding="utf-8")
    accepted = tuple(
        _verified_inline(media_id, f"https://media.example/inline-{media_id}.webp", slot)
        for slot, media_id in enumerate((301, 302, 303, 304))
    )
    working_media = MediaProgress(
        required=4,
        inline=accepted,
        featured=featured,
        layout_policy=policy,
    )
    context = {
        "post_id": post_id,
        "title": title,
        "original_link": None,
        "v2_run_id": run_id,
    }

    candidate = ProductionComposeStage(Config(), tmp_path).compose_candidate(
        context, prepared, working_media,
    )

    candidate_media = MediaProgress.from_dict(candidate["media"])
    assert [item.media_id for item in candidate_media.inline] == [301, 302, 303]
    assert [item.media_id for item in working_media.inline] == [301, 302, 303, 304]
    assert len(_embedded_media_urls(candidate["content"])) == 3
    assert "https://media.example/inline-304.webp" not in candidate["content"]

    candidate_hash = hashlib.sha256(candidate["content"].encode()).hexdigest()
    (directory / "editorial.validation.json").write_text(json.dumps({
        "_v2_run_id": run_id,
        "candidate_run_id": run_id,
        "candidate_hash": candidate_hash,
    }), encoding="utf-8")

    class Client:
        def __init__(self):
            self.post = {
                "id": post_id,
                "status": "pending",
                "title": {"raw": title},
                "content": {"raw": "<p>Conteúdo anterior.</p>"},
                "featured_media": 0,
                "meta": {},
            }
            self.updates = []
            self.uploads = []
            self.deletes = []

        def get_post(self, _post_id):
            return self.post

        def update_post(self, _post_id, update):
            self.updates.append(update)
            for key, value in update.items():
                if key == "meta":
                    self.post.setdefault("meta", {}).update(value)
                else:
                    self.post[key] = value

    client = Client()
    proposed = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
        media=MediaProgress(
            required=3,
            inline=accepted,
            featured=featured,
            layout_policy=policy,
        ),
    )
    result = WordPressWriterV2(client, tmp_path).commit(
        post_id,
        {**context, "candidate_must_be_fresh": True},
        proposed,
        Outcome.ready(),
    )

    persisted = WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"]))
    assert [item.media_id for item in persisted.media.inline] == [301, 302, 303, 304]
    assert result["applied_inline_ids"] == [301, 302, 303]
    assert len(_embedded_media_urls(client.post["content"]["raw"])) == 3
    assert not client.uploads and not client.deletes
    journal = json.loads((tmp_path / "work/v2-journal" / f"{post_id}.json").read_text())
    excess_record = next(
        row for row in journal["media_records"]
        if row["role"] == "inline" and row["media_id"] == 304
    )
    assert excess_record["destination"] == "accepted_working_not_applied"
    assert 304 not in {row["media_id"] for row in journal["apply_media_records"]}
    assert _v2_inline_media_ids_to_invalidate(working_media, persisted.media) == []


def test_compose_adds_non_distinct_featured_only_at_verified_exhausted_closure(tmp_path, monkeypatch):
    body = _long_single_sentence_body(11)
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "videogame"}],
    )
    featured = FeaturedProgress(
        FeaturedStatus.VALID, 99, "https://media.example/featured.webp",
        "f" * 64, "f" * 16, "visual:featured", {"decision": "INITIAL"},
        "Featured key art", "Crédito da imagem: Fonte oficial", 1280, 720,
    )
    editorial = {
        "title": "Notícia sobre videogame",
        "cleaned_html": body,
        "seo": {"title": "Notícia sobre videogame", "focus_keyword": "videogame"},
        "site_relevance": {"decision": "process"},
    }
    prepared, audit, error = _prepare_editorial_media_structure(
        editorial,
        WorkState(phase=Phase.EDITORIAL, media=MediaProgress(required=4, featured=featured)),
        title="Notícia sobre videogame",
    )
    assert error is None
    assert "closing_featured_repeat" in audit["media_layout_policy"]
    media = MediaProgress(
        required=4,
        inline=(
            _verified_inline(21, "https://media.example/inline-21.webp", 0),
            _verified_inline(22, "https://media.example/inline-22.webp", 1),
        ),
        featured=featured,
        search=MediaSearchProgress(
            completed=True, exhausted=True, completion_reason="EXHAUSTED_VERIFIED",
        ),
        waiver_applied=True,
        waiver_reason="search_exhausted",
        layout_policy=audit["media_layout_policy"],
    )
    candidate = ProductionComposeStage(Config(), tmp_path).compose_candidate(
        {"post_id": 115781, "title": "Notícia sobre videogame", "original_link": None},
        prepared,
        media,
    )
    assert candidate["content"].count("<img") == 3
    assert candidate["content"].rfind("featured.webp") > candidate["content"].rfind("inline-22.webp")
    assert candidate["media"]["closing_featured_repeat"]["media_id"] == 99
    assert candidate["media"]["inline"]["accepted_count"] == 2


def test_skip_decision_bypasses_media_structure_requirements():
    short_skip = {
        "decision": "skip",
        "cleaned_html": "<p>Não é uma notícia processável.</p>",
        "media_plan": [],
    }
    prepared, audit, error = _prepare_editorial_media_structure(
        short_skip,
        WorkState(phase=Phase.EDITORIAL),
        title="Item não relacionado",
    )
    assert error is None
    assert audit["status"] == "not_applicable"
    assert audit["decision"] == "skip"
    assert "media_layout_policy" not in audit
    assert prepared["cleaned_html"] == short_skip["cleaned_html"]


def test_structure_audit_hash_mismatch_rebuilds_prepared_layout(tmp_path, monkeypatch):
    post_id = 115783
    title = "Atualização semanal de videogame"
    body = _long_single_sentence_body(10)
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "videogame"}],
    )
    prepared, audit, error = _prepare_editorial_media_structure(
        {"cleaned_html": body, "seo": {}},
        WorkState(phase=Phase.EDITORIAL),
        title=title,
    )
    assert error is None
    assert audit["structure_sha256"]
    audit["structure_sha256"] = "0" * 64
    directory = tmp_path / "backups" / str(post_id)
    directory.mkdir(parents=True)
    (directory / "editorial.structure.json").write_text(json.dumps(audit), encoding="utf-8")
    (directory / "editorial.draft.json").write_text(json.dumps(prepared), encoding="utf-8")

    editorial = dict(prepared)
    editorial, rebuilt_audit, policy = _ensure_prepared_media_structure(
        tmp_path,
        post_id,
        editorial,
        WorkState(phase=Phase.MEDIA),
        title=title,
    )
    assert rebuilt_audit["structure_sha256"] != "0" * 64
    assert rebuilt_audit["prepared_body_sha256"]
    assert policy["target_effective"] == 3
    saved = json.loads((directory / "editorial.structure.json").read_text(encoding="utf-8"))
    assert saved["structure_sha256"] == rebuilt_audit["structure_sha256"]


def test_editorial_rework_includes_html_semantics_gate_and_omits_media_gate(tmp_path, monkeypatch):
    post_id = 115781
    title = "Notícia sobre videogame"
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"posts": [{"post_id": post_id, "title": title}]}), encoding="utf-8")
    directory = tmp_path / "backups" / str(post_id)
    directory.mkdir(parents=True)
    old_editorial = {"title": title, "cleaned_html": _substantive_body(5), "seo": {}}
    (directory / "editorial.draft.json").write_text(json.dumps(old_editorial), encoding="utf-8")
    (directory / "editorial.validation.json").write_text(json.dumps({
        "failures": [
            {"gate": "html_semantics", "detail": "empty paragraph"},
            {"gate": "imagens_no_corpo", "detail": "missing media"},
        ],
    }), encoding="utf-8")
    (directory / "editorial.structure.json").write_text(json.dumps({
        "status": "rework_required", "failure": "unplaceable planned media",
    }), encoding="utf-8")

    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.prepare_batch",
        lambda *_args: {"prepared": 1, "editorial_input": str(input_path)},
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "videogame"}],
    )
    captured = {}

    def generate(path, **_kwargs):
        captured["input"] = json.loads(Path(path).read_text(encoding="utf-8"))
        return {"output": "generated.json"}

    monkeypatch.setattr("unicornio_editor.pipeline_v2.production_stages.generate_editorial_batch", generate)
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.load_editorial_batch",
        lambda *_args: {"items": [{"post_id": post_id, "status": "ok", "editorial": {
            "title": title,
            "cleaned_html": _substantive_body(6),
            "seo": {"title": title, "meta_description": "Descrição editorial suficientemente longa para a validação.", "focus_keyword": "videogame"},
            "site_relevance": {"decision": "process", "confidence": 0.95, "reason": "games", "matched_topics": ["games"]},
        }}]},
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.resolve_editorial_defaults",
        lambda editorial, _post: editorial,
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.validate_editorial",
        lambda editorial, **_kwargs: editorial,
    )
    editorial_config = SimpleNamespace(
        editorial_api_key="test", editorial_base_url="https://editorial.test",
        editorial_model="test", http_timeout=1, min_relevance_confidence=0.8,
    )
    result = ProductionEditorialStage(object(), editorial_config, tmp_path)(
        {"post_id": post_id, "title": title, "post": {"id": post_id, "title": {"raw": title}}},
        WorkState(phase=Phase.EDITORIAL, blocker=BlockerCode.STRUCTURE),
    )
    rework = captured["input"]["posts"][0]["rework"]
    gates = {failure["gate"] for failure in rework["failed_gates"]}
    assert "html_semantics" in gates
    assert "estrutura" in gates
    assert "imagens_no_corpo" not in gates
    assert result["site_relevance"]["decision"] == "process"


def test_validate_recomposes_candidate_when_resumed_body_changed(tmp_path, monkeypatch):
    body = _long_single_sentence_body(10) + "<p><br></p>"
    title = "Atualização semanal de videogame"
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.post_subjects",
        lambda **_kwargs: [{"subject": "videogame"}],
    )
    monkeypatch.setattr(
        "unicornio_editor.pipeline_v2.production_stages.compose_final_content",
        lambda editorial, *_args, **_kwargs: (editorial["cleaned_html"], None, "not_required"),
    )
    observed = {}

    def checklist(**kwargs):
        observed["editorial"] = kwargs["editorial"]
        observed["content"] = kwargs["content"]
        observed["media"] = kwargs["media_context"]
        return {"all_passed": True, "items": []}

    monkeypatch.setattr("unicornio_editor.pipeline_v2.production_stages.run_pre_publish_checklist", checklist)
    source = {
        "title": title,
        "cleaned_html": body,
        "seo": {"title": title, "meta_description": "Descrição suficientemente longa para este teste de validação local.", "focus_keyword": "videogame"},
        "site_relevance": {"decision": "process", "confidence": 0.95, "reason": "games", "matched_topics": ["games"]},
    }
    candidate = {
        "_v2_run_id": "previous-run",
        "content": "<p>Candidato antigo que não corresponde ao corpo preparado.</p>",
        "editorial": {"cleaned_html": "<p>Texto antigo.</p>", "seo": {}},
        "media": MediaProgress(required=4).to_dict(),
        "featured_media": None,
    }
    result = ProductionValidateStage(object(), Config(), tmp_path)(
        {
            "post_id": 115782,
            "title": title,
            "original_link": None,
            "post": {"id": 115782, "status": "pending", "title": {"raw": title}, "meta": {}},
            "editorial": source,
            "candidate_must_be_fresh": True,
            "v2_run_id": "current-run",
        },
        candidate,
    )
    assert result["passed"] is True
    assert observed["editorial"]["cleaned_html"] == _long_single_sentence_body(10)
    assert "Candidato antigo" not in observed["content"]
    assert observed["media"]["inline"]["required"] == 3
    assert observed["media"]["layout_policy"]["target_ideal"] == 4
    assert observed["media"]["layout_policy"]["target_effective"] == 3


def test_listicle_media_preparation_does_not_restructure_item_coverage():
    title = "Top 3 jogos para conhecer"
    body = "".join(
        f"<h2>{index}. Jogo {index}</h2><p>Descrição editorial do jogo {index} com detalhes para leitores.</p>"
        for index in range(1, 4)
    )
    source = {"cleaned_html": body + "<p><br></p>", "categories": [], "seo": {}}
    prepared, audit, error = _prepare_editorial_media_structure(
        source,
        WorkState(phase=Phase.EDITORIAL),
        title=title,
    )
    assert error is None
    assert audit["layout"] == "listicle_preserved"
    assert audit["paragraphs_after"] == 3
    assert prepared["cleaned_html"].count("<h2>") == 3
    assert "<p><br></p>" not in prepared["cleaned_html"]


def test_media_structure_counts_only_positions_not_already_present_and_verified():
    existing_urls = ("https://example.test/existing-1.webp", "https://example.test/existing-2.webp")
    sentence = "Parágrafo com contexto editorial suficiente sobre videogame e os detalhes confirmados nesta semana. "
    body = "".join(
        f"<p>{sentence * 8}{index}.</p>"
        for index in range(8)
    )
    html = body + "".join(f'<img src="{url}" />' for url in existing_urls)
    media = MediaProgress(
        required=4,
        inline=tuple(
            _verified_inline(600 + index, url, index)
            for index, url in enumerate(existing_urls)
        ),
    )
    state = WorkState(phase=Phase.EDITORIAL, media=media)
    prepared, audit, error = _prepare_editorial_media_structure(
        {"cleaned_html": html, "seo": {}, "categories": []},
        state,
        title="Notícia sobre videogame",
    )
    assert error is None
    assert audit["valid_existing_inline"] == 2
    assert audit["media_positions_needed"] == 2
    assert audit["paragraphs_after"] == 8
    assert prepared["cleaned_html"].count("<img") == 2


def test_checklist_editorial_failure_without_phase_is_left_for_gate_mapping():
    failures = _checklist_failures({"items": [{"name": "qualidade_texto", "status": "fail", "detail": "invalid"}]})
    assert failures == [{"gate": "qualidade_texto", "detail": "invalid"}]


def test_checklist_error_is_propagated_as_media_provider_failure():
    failures = _checklist_failures({"items": [{
        "name": "featured_url", "status": "error", "detail": "attachment read failed",
        "blocker": "provider_error", "phase": "media_validation",
    }]})
    assert failures == [{
        "gate": "featured_url", "detail": "attachment read failed",
        "blocker": "provider_error", "phase": "media",
    }]


def test_compose_and_validate_are_real_adapters(tmp_path, monkeypatch):
    editorial = {
        "cleaned_html": _substantive_body(6),
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
    paragraph = (
        "A equipe responsável pelo projeto confirmou nesta semana que a atualização amplia os recursos "
        "disponíveis, melhora o desempenho nas principais plataformas e corrige problemas relatados pelos "
        "jogadores desde o lançamento. Os desenvolvedores também explicaram como cada mudança será "
        "distribuída, quais opções permanecem configuráveis e de que maneira o público poderá acompanhar "
        "as próximas etapas do trabalho."
    )
    editorial = {
        "cleaned_html": "".join(f"<p>{paragraph}</p>" for _ in range(13)),
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
            "<p>Parágrafo com contexto editorial suficiente para explicar os detalhes conhecidos da notícia ao público interessado.</p>"
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
        credit_text="Crédito da imagem: Featured Source.",
    )
    media = MediaProgress(required=2, inline=(duplicate, distinct), featured=featured)

    candidate = ProductionComposeStage(Config(), tmp_path)(
        {"post_id": 22, "title": "Notícia", "original_link": None}, editorial, media,
    )

    assert duplicate_url not in candidate["content"]
    assert "Legenda antiga" not in candidate["content"]
    assert distinct_url in candidate["content"]
    assert candidate["content"].count("Crédito da imagem: Featured Source.") == 1
    accepted_ids = [row["media_id"] for row in candidate["media"]["inline"]["accepted"]]
    assert accepted_ids == [22]


def test_resume_composes_credit_for_accepted_media_from_library_index_without_upload(
    tmp_path, monkeypatch,
):
    import unicornio_editor.cli as cli_module
    import unicornio_editor.pipeline_v2.runtime as runtime_module
    import unicornio_editor.pipeline_v2.visual_media as visual_media_module

    first = replace(_verified_inline(303, "https://wp.test/a.webp", 0), credit_text="")
    second = replace(_verified_inline(304, "https://wp.test/b.webp", 3), credit_text="")
    featured = FeaturedProgress(
        FeaturedStatus.VALID,
        399,
        "https://wp.test/featured.webp",
        "f" * 64,
        "f" * 16,
        "visual:" + "f" * 24,
        {"decision": "INITIAL"},
        "Imagem de capa",
        "",
        1200,
        800,
    )
    previous = MediaProgress(required=2, inline=(first, second), featured=featured)
    state = SimpleNamespace(media=previous, phase=Phase.MEDIA)
    register_library_media(
        tmp_path,
        source_url="https://images.ign.com/a.webp?signature=secret",
        source_page="https://user:private@www.ign.com/article?token=private",
        media_id=first.media_id,
    )
    register_library_media(
        tmp_path,
        source_url="https://images.nintendo.com/featured.webp?signature=secret",
        source_page="https://user:private@www.nintendo.com/article?token=private",
        media_id=featured.media_id,
    )
    record_v2_observation(
        tmp_path, post_id=303, run_id="resume-accepted-source", state=state,
        phase="media", applied=False,
    )

    monkeypatch.setattr(runtime_module, "_required_image_count_for_editorial", lambda *_a, **_k: 2)
    monkeypatch.setattr(
        cli_module,
        "_resolve_media_batch",
        lambda *_a, **_k: pytest.fail("accepted media recovery must not upload or search"),
    )
    monkeypatch.setattr(
        visual_media_module,
        "materialize_media_identity",
        lambda _client, _config, _root, media, **_kwargs: media,
    )

    class Client:
        def get_media(self, _media_id):
            raise AssertionError("accepted media recovery must not issue a new WP read")

    body = _substantive_body(8)
    editorial = {"cleaned_html": body, "seo": {"focus_keyword": "Nintendo"}}
    context = {
        "post_id": 303,
        "title": "Nintendo Switch: atualização",
        "post": {"featured_media": featured.media_id, "content": {"raw": ""}},
    }
    recovered = ProductionMediaResolver(Client(), Config(), tmp_path)(
        context, state, editorial, previous,
    )

    assert recovered.inline[0].credit_text == "Crédito da imagem: IGN."
    assert recovered.inline[1].credit_text == ""
    assert recovered.featured.credit_text == "Crédito da imagem: Nintendo."
    candidate = ProductionComposeStage(Config(), tmp_path).compose_candidate(
        context, editorial, recovered, persist=False,
    )

    assert "Crédito da imagem: IGN." in candidate["content"]
    assert candidate["content"].count("Crédito da imagem: IGN.") == 1
    assert candidate["content"].count("Crédito da imagem: Nintendo.") == 1
    assert "https://wp.test/a.webp" in candidate["content"]
    assert [item["media_id"] for item in candidate["media"]["inline"]["accepted"]] == [303, 304]
    with connect_media_registry(tmp_path) as db:
        statuses = db.execute(
            "SELECT status FROM media_usage WHERE post_id=? AND role='inline' ORDER BY wordpress_media_id",
            (303,),
        ).fetchall()
        featured_status = db.execute(
            "SELECT status FROM media_usage WHERE post_id=? AND role='featured' AND wordpress_media_id=?",
            (303, featured.media_id),
        ).fetchone()[0]
    assert [row[0] for row in statuses] == ["accepted", "accepted"]
    assert featured_status == "accepted"


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
