import json
from types import SimpleNamespace

from unicornio_editor.pipeline_v2.migration import repair_schema_115142
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


class Client:
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


def fixture(tmp_path):
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
        for index, media_id in enumerate((115156, 115157, 115158, 115159, 115160, 115161))
    )
    media = MediaProgress(
        required=6,
        inline=inline,
        featured=FeaturedProgress(FeaturedStatus.VALID, 115155, "https://media.example/115155.webp"),
    )
    state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.EDITORIAL,
        blocker=BlockerCode.SCHEMA,
        retry=RetryInfo(attempts=1, phase_attempts=1, policy_version=3),
        relevance_approved=True,
        media=media,
    )
    body = "<p>Este artigo apresenta os detalhes conhecidos sobre o lançamento e seus personagens.</p>"
    draft = {
        "decision": "process",
        "localization": {"source_language": "pt-BR"},
        "site_relevance": {"decision": "process", "confidence": 0.95, "reason": "games", "matched_topics": ["games"]},
        "cleaned_html": body,
        "title": "Detalhes do lançamento do jogo",
        "seo": {
            "title": "Detalhes do lançamento do jogo",
            "meta_description": "Confira os detalhes conhecidos sobre o lançamento, personagens e recursos do jogo nesta atualização editorial em português.",
            "focus_keyword": "lançamento do jogo",
        },
        "media_plan": [],
        "needs_trailer": False,
        "trailer_url": None,
        "game_name": None,
    }
    directory = tmp_path / "backups" / "115142"
    directory.mkdir(parents=True)
    (directory / "editorial.draft.json").write_text(json.dumps(draft), encoding="utf-8")
    (directory / "editorial.error.json").write_text(
        json.dumps({"error": "editorial schema", "unknown": ["localization"]}), encoding="utf-8"
    )
    journal = tmp_path / "work" / "v2-journal"
    journal.mkdir(parents=True)
    (journal / "115142.json").write_text(
        json.dumps({"post_id": 115142, "status": "validated", "run_id": "old-run"}),
        encoding="utf-8",
    )
    post = {
        "id": 115142,
        "status": "pending",
        "title": {"raw": "Detalhes do lançamento do jogo"},
        "content": {"raw": "<p>Conteúdo anterior.</p>"},
        "meta": {"_hermes_work_state": json.dumps(state.to_dict())},
    }
    attachments = {
        item.media_id: {"source_url": item.media_url, "media_details": {"mime_type": "image/webp"}}
        for item in inline
    }
    attachments[115155] = {
        "source_url": "https://media.example/115155.webp",
        "media_details": {"mime_type": "image/webp"},
    }
    return Client(post, attachments), state, draft


def config():
    return SimpleNamespace(min_relevance_confidence=0.8)


def test_schema_115142_dry_run_is_exact_and_read_only(tmp_path):
    client, state, draft = fixture(tmp_path)
    result = repair_schema_115142(client, config(), tmp_path, post_id=115142, apply=False)

    assert result["eligible"] is True
    assert result["checks"]["removed_fields"] == ["decision", "localization"]
    assert result["preserve"]["inline_media_ids"] == [115156, 115157, 115158, 115159, 115160, 115161]
    assert client.updates == []
    assert json.loads((tmp_path / "backups/115142/editorial.draft.json").read_text()) == draft
    assert state.retry.attempts == 1


def test_schema_115142_apply_only_reopens_compose_and_preserves_media(tmp_path):
    client, state, draft = fixture(tmp_path)
    result = repair_schema_115142(client, config(), tmp_path, post_id=115142, apply=True)

    assert result["readback"] is True
    assert result["reopened_state"]["state"] == "pending"
    assert result["reopened_state"]["phase"] == "compose"
    assert result["reopened_state"]["media"] == state.media.to_dict()
    persisted = json.loads((tmp_path / "backups/115142/editorial.draft.json").read_text())
    assert set(draft) - set(persisted) == {"decision", "localization"}
    assert len(client.updates) == 1


def test_schema_repair_refuses_other_post_without_writing(tmp_path):
    client, _state, _draft = fixture(tmp_path)
    result = repair_schema_115142(client, config(), tmp_path, post_id=115141, apply=True)
    assert result["eligible"] is False
    assert result["reason"] == "post_id_not_allowlisted"
    assert client.updates == []
