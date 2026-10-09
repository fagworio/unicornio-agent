import json
from types import SimpleNamespace

from unicornio_editor.pipeline_v2.migration import repair_compose_114987
from unicornio_editor.pipeline_v2.model import (
    BlockerCode,
    FeaturedProgress,
    FeaturedStatus,
    InlineMedia,
    LifecycleState,
    MediaProgress,
    Phase,
    RetryInfo,
    WorkState,
)


class _Client:
    def __init__(self, post, attachments):
        self.post = post
        self.attachments = attachments
        self.updates = []

    def get_post(self, _post_id):
        return self.post

    def get_media(self, media_id):
        return self.attachments[media_id]

    def update_post(self, post_id, payload):
        self.updates.append((post_id, payload))
        self.post["meta"].update(payload["meta"])


def _fixture(tmp_path):
    long_body = (
        "A equipe confirmou uma atualização importante para o jogo nesta semana, "
        "com detalhes sobre personagens, sistemas e conteúdo adicional para os jogadores. "
        "O anúncio também informa que o lançamento seguirá o calendário divulgado anteriormente, "
        "sem alterar os recursos já apresentados pela desenvolvedora."
    )
    html = f"<p>{long_body}</p><p>{long_body}</p><p>{long_body}</p>" + "".join(
        f"<p>O parágrafo {index} explica os detalhes conhecidos até o momento.</p>"
        for index in range(1, 7)
    )
    inline = tuple(
        InlineMedia(
            media_id,
            f"https://media.example/{media_id}.webp",
            index * 3,
            f"Imagem {index}",
            f"Crédito da imagem: Fonte {index}",
            f"assunto {index}",
            width=1280,
            height=720,
        )
        for index, media_id in enumerate((115134, 115135, 115136, 115137))
    )
    media = MediaProgress(
        required=4,
        inline=inline,
        featured=FeaturedProgress(FeaturedStatus.VALID, 115138, "https://media.example/featured.webp"),
    )
    state = WorkState(
        state=LifecycleState.HUMAN_REQUIRED,
        phase=Phase.COMPOSE,
        blocker=BlockerCode.MANIFEST_INVALID,
        retry=RetryInfo(attempts=6, phase_attempts=2, policy_version=3),
        relevance_approved=True,
        media=media,
    )
    draft = {
        "site_relevance": {"decision": "process", "confidence": 0.95, "reason": "games", "matched_topics": ["games"]},
        "cleaned_html": html,
        "media_plan": [],
        "needs_trailer": False,
        "trailer_url": None,
        "game_name": None,
    }
    backup = tmp_path / "backups" / "114987"
    backup.mkdir(parents=True)
    (backup / "editorial.draft.json").write_text(json.dumps(draft), encoding="utf-8")
    (backup / "editorial.partial.json").write_text(json.dumps(media.to_dict()), encoding="utf-8")
    journal = tmp_path / "work" / "v2-journal"
    journal.mkdir(parents=True)
    (journal / "114987.json").write_text(
        json.dumps({"post_id": 114987, "status": "committing", "run_id": "old-run"}),
        encoding="utf-8",
    )
    post = {
        "id": 114987,
        "status": "pending",
        "title": {"raw": "Atualização do jogo"},
        "content": {"raw": "<p>Conteúdo antigo.</p>"},
        "meta": {"_hermes_work_state": json.dumps(state.to_dict())},
    }
    attachments = {
        media_id: {"source_url": item.media_url, "media_details": {"mime_type": "image/webp"}}
        for media_id, item in [(item.media_id, item) for item in inline]
    }
    return _Client(post, attachments), draft, state


def test_repair_114987_dry_run_reconstructs_without_writing(tmp_path):
    client, draft, state = _fixture(tmp_path)
    config = SimpleNamespace(internal_links_enabled=False, http_timeout=1, min_relevance_confidence=0.8)

    result = repair_compose_114987(client, config, tmp_path, apply=False)

    assert result["eligible"] is True
    assert result["restructure"]["original_paragraphs"] == 9
    assert result["restructure"]["final_paragraphs"] == 12
    assert result["words"]["before"] == result["words"]["after"]
    assert result["validation"]["all_accepted_media_reconstructed"] is True
    assert result["validation"]["distinct_accepted_img_elements"] == 4
    assert result["validation"]["credits_present"] is True
    assert result["validation"]["featured_valid"] is True
    assert result["validation"]["language_ok"] is True
    assert client.updates == []
    assert json.loads((tmp_path / "backups/114987/editorial.draft.json").read_text()) == draft
    assert result["preserve"]["attempts"] == state.retry.attempts


def test_repair_114987_apply_persists_only_draft_and_v2_reopen(tmp_path):
    client, _draft, state = _fixture(tmp_path)
    config = SimpleNamespace(internal_links_enabled=False, http_timeout=1, min_relevance_confidence=0.8)

    result = repair_compose_114987(client, config, tmp_path, apply=True)

    assert result["eligible"] is True
    assert result["readback"] is True
    assert result["reopened_state"]["state"] == "pending"
    assert result["reopened_state"]["phase"] == "compose"
    assert result["reopened_state"]["retry"]["attempts"] == state.retry.attempts
    assert result["reopened_state"]["media"] == state.media.to_dict()
    saved = json.loads((tmp_path / "backups/114987/editorial.draft.json").read_text())
    assert saved["cleaned_html"].count("</p>") == 12
    assert list((tmp_path / "backups/114987").glob("editorial.draft.compose-recovery.*.json"))
    assert len(client.updates) == 1
    assert set(client.updates[0][1]) == {"meta"}
