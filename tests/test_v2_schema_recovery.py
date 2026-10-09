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
        json.dumps({
            "post_id": 115142,
            "status": "committed",
            "readback": True,
            "candidate_fresh": True,
            "candidate_run_id": "real-run",
            "state": state.to_dict(),
            "detail": "top-level has invalid fields (unknown=['localization'])",
        }),
        encoding="utf-8",
    )
    (directory / "editorial.candidate.json").write_text(
        json.dumps({"_v2_run_id": "real-run", "content": body}), encoding="utf-8"
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
    assert result["historical_error_source"] == "journal"
    assert result["journal_identity_verified"] is True
    assert result["candidate_run_id_verified"] is True
    assert result["journal_state_matches_wordpress"] is True
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


def test_schema_repair_uses_committed_journal_without_error_artifact(tmp_path):
    client, _state, _draft = fixture(tmp_path)
    (tmp_path / "backups/115142/editorial.error.json").unlink()
    result = repair_schema_115142(client, config(), tmp_path, post_id=115142)
    assert result["eligible"] is True
    assert result["historical_error_source"] == "journal"


def test_schema_repair_rejects_inconsistent_journal_identity(tmp_path):
    client, _state, _draft = fixture(tmp_path)
    journal_path = tmp_path / "work/v2-journal/115142.json"
    journal = json.loads(journal_path.read_text())
    journal["candidate_run_id"] = "different-run"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    result = repair_schema_115142(client, config(), tmp_path, post_id=115142)
    assert result["eligible"] is False
    assert result["historical_error"] is False
    assert result["reason"] == "signature_or_checkpoints_not_verified"


def test_schema_repair_rejects_journal_from_another_post(tmp_path):
    client, _state, _draft = fixture(tmp_path)
    journal_path = tmp_path / "work/v2-journal/115142.json"
    journal = json.loads(journal_path.read_text())
    journal["post_id"] = 999999
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    result = repair_schema_115142(client, config(), tmp_path, post_id=115142)
    assert result["eligible"] is False
    assert result["journal_identity_verified"] is False


def test_schema_repair_rejects_running_or_wrong_detail_journal(tmp_path):
    for index, (status, readback, detail) in enumerate((
        ("running", True, "top-level has invalid fields (unknown=['localization'])"),
        ("committed", False, "top-level has invalid fields (unknown=['localization'])"),
        ("committed", True, "top-level has invalid fields (unknown=['decision'])"),
    )):
        root = tmp_path / str(index)
        client, state, _draft = fixture(root)
        journal_path = root / "work/v2-journal/115142.json"
        journal = json.loads(journal_path.read_text())
        journal.update({"status": status, "readback": readback, "detail": detail})
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        result = repair_schema_115142(client, config(), tmp_path, post_id=115142)
        assert result["eligible"] is False
        assert result["historical_error"] is False
        # Keep the loop's state object used, making the fixture explicit and
        # ensuring the refusal is not caused by a missing WordPress state.
        assert state.state is LifecycleState.PENDING


def test_schema_repair_rejects_attachment_url_divergence(tmp_path):
    client, _state, _draft = fixture(tmp_path)
    client.attachments[115156]["source_url"] = "https://media.example/other.webp"
    result = repair_schema_115142(client, config(), tmp_path, post_id=115142)
    assert result["eligible"] is False
    assert result["checks"]["attachments_valid"] is False
    assert result["attachments"][0]["persisted_url_matches"] is False


def test_schema_repair_rejects_wordpress_state_divergence(tmp_path):
    client, state, _draft = fixture(tmp_path)
    divergent = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        blocker=BlockerCode.SCHEMA,
        retry=state.retry,
        relevance_approved=state.relevance_approved,
        media=state.media,
    )
    client.post["meta"]["_hermes_work_state"] = json.dumps(divergent.to_dict())
    result = repair_schema_115142(client, config(), tmp_path, post_id=115142)
    assert result["eligible"] is False
    assert result["journal_state_matches_wordpress"] is False


def test_schema_repair_does_not_rollback_draft_after_ambiguous_meta_update(tmp_path):
    client, _state, draft = fixture(tmp_path)

    class ReadbackFails(Client):
        def __init__(self, post, attachments):
            super().__init__(post, attachments)
            self.reads = 0

        def get_post(self, post_id):
            self.reads += 1
            if self.reads >= 3:
                raise RuntimeError("readback unavailable")
            return super().get_post(post_id)

    client = ReadbackFails(client.post, client.attachments)
    result = repair_schema_115142(client, config(), tmp_path, post_id=115142, apply=True)

    assert result["eligible"] is False
    assert result["reason"] == "reconciliation_required"
    persisted = json.loads((tmp_path / "backups/115142/editorial.draft.json").read_text())
    assert set(draft) - set(persisted) == {"decision", "localization"}
    assert len(client.updates) == 1


def test_schema_repair_refuses_draft_changed_between_scan_and_apply(tmp_path):
    base, _state, draft = fixture(tmp_path)

    class DraftChangesOnSecondRead(Client):
        def __init__(self, post, attachments, path):
            super().__init__(post, attachments)
            self.reads = 0
            self.path = path

        def get_post(self, post_id):
            self.reads += 1
            if self.reads == 2:
                changed = dict(draft)
                changed["title"] = "Draft alterado durante o apply"
                self.path.write_text(json.dumps(changed), encoding="utf-8")
            return super().get_post(post_id)

    draft_path = tmp_path / "backups/115142/editorial.draft.json"
    client = DraftChangesOnSecondRead(base.post, base.attachments, draft_path)
    result = repair_schema_115142(client, config(), tmp_path, post_id=115142, apply=True)

    assert result["eligible"] is False
    assert result["reason"] == "changed_after_scan"
    assert client.updates == []


def test_schema_repair_is_idempotent_after_success(tmp_path):
    client, _state, _draft = fixture(tmp_path)
    first = repair_schema_115142(client, config(), tmp_path, post_id=115142, apply=True)
    second = repair_schema_115142(client, config(), tmp_path, post_id=115142, apply=True)
    assert first["readback"] is True
    assert second["eligible"] is False
    assert len(client.updates) == 1
