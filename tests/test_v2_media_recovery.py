import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unicornio_editor.media.visual_identity import VisualIdentity, VisualIdentityDecision
from unicornio_editor.pipeline_v2.media_recovery import (
    adopt_existing_attachment,
    recover_historical_attachment,
)
from unicornio_editor.pipeline_v2.model import (
    BlockerCode, FeaturedProgress, FeaturedStatus, InlineMedia, LifecycleState,
    MediaProgress, Phase, RetryInfo, WorkState,
)


def _verified(media_id, slot, marker, decision="DIFFERENT"):
    return InlineMedia(
        media_id, f"https://wp.test/{media_id}.webp", slot,
        sha256=marker * 64, phash=f"{int(marker, 16):016x}",
        visual_group_id=f"visual:{marker * 24}",
        visual_verification={"decision": decision, "duplicate_of": "115170" if decision.startswith("SAME") else ""},
        subject="Jogo", credit_text="Crédito da imagem: Fonte",
    )


def _fixture(tmp_path):
    featured = FeaturedProgress(
        FeaturedStatus.VALID, 115170, "https://wp.test/115170.webp",
        "a" * 64, "a" * 16, "visual:" + "a" * 24, {"decision": "INITIAL"},
    )
    media = MediaProgress(
        required=4,
        inline=(
            _verified(115171, 0, "b", "SAME_ART_CROP"),
            _verified(115172, 1, "c", "SAME_ART_CROP"),
            _verified(115173, 2, "d"),
        ),
        featured=featured,
    )
    state = WorkState(
        state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA,
        blocker=BlockerCode.INTERNAL_ERROR,
        retry=RetryInfo(attempts=35, no_progress=28, next_at="2030-01-01T00:00:00+00:00"),
        relevance_approved=True, media=media,
    )
    root = tmp_path / "install"
    backup = root / "backups" / "115152"
    backup.mkdir(parents=True)
    paragraph = "O jogo apresenta personagens novos e detalhes importantes do combate nesta atualização."
    (backup / "editorial.draft.json").write_text(json.dumps({
        "cleaned_html": f"<p>{paragraph}</p>" * 8,
        "seo": {"title": "Jogo", "focus_keyword": "Jogo"},
    }), encoding="utf-8")
    evidence = {
        "version": 1, "post_id": 115152, "media_id": 115305,
        "run_id": "old-canary-run", "candidate_id": "historical-candidate-115305",
        "source_page_url": "https://source.test/article",
        "direct_image_url": "https://source.test/jogo-art.webp",
        "subject": "Jogo", "alt_text": "Arte de Jogo",
        "credit_text": "Crédito da imagem: Fonte original",
        "sha256": "e" * 64, "phash": "e" * 16, "visual_group_id": "visual:" + "e" * 24,
        "evidence": {"verdict": "deterministic_match", "score": 8, "matched": ["filename", "heading"]},
        "visual_verification": {"decision": "DIFFERENT", "confidence": 0.95},
    }
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    candidate_content = "<p>Artigo aprovado no run histórico.</p>"
    (backup / "editorial.candidate.json").write_text(json.dumps({
        "_v2_run_id": evidence["run_id"],
        "content": candidate_content,
    }), encoding="utf-8")
    journal_dir = root / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "115152.json").write_text(json.dumps({
        "run_id": evidence["run_id"],
        "candidate_run_id": evidence["run_id"],
        "candidate_hash": hashlib.sha256(candidate_content.encode()).hexdigest(),
    }), encoding="utf-8")
    from unicornio_editor.media.registry import record_v2_candidate_decision
    record_v2_candidate_decision(
        root, post_id=115152, run_id=evidence["run_id"],
        candidate={
            "candidate_id": evidence["candidate_id"], "subject": evidence["subject"],
            "source_page_url": evidence["source_page_url"],
            "direct_image_url": evidence["direct_image_url"],
            "evidence": evidence["evidence"],
        }, role="inline", disposition="accepted", reason_code="candidate_accepted",
    )

    class Client:
        def __init__(self):
            self.post = {
                "id": 115152, "status": "pending", "title": {"raw": "Jogo"},
                "content": {"raw": "<p>Conteúdo original preservado.</p>"},
                "featured_media": 115170, "meta": {
                    "_hermes_work_state": json.dumps(state.to_dict()), "other": "preserved",
                    "_hermes_human_reopened_at": "2030-01-01T00:00:00+00:00",
                    "_hermes_human_reopened_from": "awaiting_human",
                },
            }
            self.updates = []
            self.get_calls = 0
            self.before_second_get = None

        def get_post(self, _post_id):
            self.get_calls += 1
            if self.get_calls == 2 and self.before_second_get:
                self.before_second_get(self.post)
            return self.post

        def get_media(self, media_id):
            return {
                "id": media_id, "source_url": f"https://wp.test/{media_id}.webp",
                "mime_type": "image/webp", "media_details": {"width": 960, "height": 540},
                "title": {"rendered": "Jogo"}, "caption": {"rendered": "Crédito da imagem: Fonte"},
            }

        def update_post(self, _post_id, payload):
            assert set(payload) == {"meta"}
            assert set(payload["meta"]) == {"_hermes_work_state"}
            self.updates.append(payload)
            self.post["meta"].update(payload["meta"])

    config = SimpleNamespace(
        dry_run=False, remote_url_policy="off", http_timeout=1,
        visual_comparison_min_confidence=0.85, visual_comparison_max_calls=4,
        vision_enabled=True, vision_api_key="test-key",
    )
    return root, state, evidence, evidence_path, Client(), config


def _mock_validation(monkeypatch, tmp_path):
    import unicornio_editor.pipeline_v2.media_recovery as recovery

    def download(_url, destination, **_kwargs):
        path = tmp_path / destination.name
        path.write_bytes(b"fixture")
        return path

    def fingerprint(path):
        name = str(path)
        if "attachment-115305" in name:
            return VisualIdentity("e" * 64, "e" * 16, "visual:" + "e" * 24)
        marker = {"115171": "b", "115172": "c", "115173": "d", "115170": "a"}.get(
            next((media_id for media_id in ("115171", "115172", "115173", "115170") if f"baseline-{media_id}" in name), "")
        )
        if marker:
            return VisualIdentity(marker * 64, marker * 16, "visual:" + marker * 24)
        raise AssertionError(path)

    monkeypatch.setattr(recovery, "download_image", download)
    monkeypatch.setattr(recovery, "fingerprint_path", fingerprint)
    monkeypatch.setattr(recovery, "validate_discovered_candidate", lambda *_a, **_k: {
        "valid": True, "source_verified": True, "verification_level": "EXACT_URL",
    })
    monkeypatch.setattr(recovery, "verify_downloaded_against_source", lambda **_k: (True, "same frame"))
    observed_roots = []

    def verify_identity(*_args, **kwargs):
        cache_root = Path(kwargs["root"])
        observed_roots.append(cache_root)
        cache_file = cache_root / "work" / "visual_comparison_cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text('{"simulated-comparison": {"decision": "DIFFERENT"}}')
        return (
        VisualIdentity("e" * 64, "e" * 16, "visual:" + "e" * 24),
        VisualIdentityDecision("DIFFERENT", True, reason="fixture", comparisons=({"confidence": 0.95},)),
        )

    def validate_plan(*_args, **kwargs):
        observed_roots.append(kwargs["root"])
        return {"valid": 1, "rejected": []}

    monkeypatch.setattr(recovery, "verify_candidate_identity", verify_identity)
    monkeypatch.setattr(recovery, "validate_media_plan", validate_plan)
    return observed_roots


def test_user_supplied_legacy_proof_cannot_bypass_historical_linkage(tmp_path, monkeypatch):
    root, _original, evidence, evidence_path, client, config = _fixture(tmp_path)
    evidence["legacy_proof"] = {
        "upload_event": {"post_id": 115152, "media_id": 115305, "event_id": "upload-1"},
        "attachment_get": {"post_id": 115152, "media_id": 115305},
        "source_record": {
            "source_page_url": evidence["source_page_url"],
            "direct_image_url": evidence["direct_image_url"],
        },
        "run_binding": {"historical_run_id": evidence["run_id"], "upload_event_id": "upload-1"},
    }
    candidate_path = root / "backups/115152/editorial.candidate.json"
    candidate = json.loads(candidate_path.read_text())
    candidate["_v2_run_id"] = "forged-other-run"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(ValueError, match="historical V2 candidate and journal"):
        recover_historical_attachment(
            client, config, root, post_id=115152, media_id=115305,
            evidence_path=evidence_path,
        )
    assert client.updates == []


def test_recovery_preview_is_read_only_and_preserves_human_lifecycle(tmp_path, monkeypatch):
    root, original, _evidence, evidence_path, client, config = _fixture(tmp_path)
    observed_roots = _mock_validation(monkeypatch, tmp_path)
    historical_journal = (root / "work/v2-journal/115152.json").read_text()
    registry_before = (root / "work/media_registry.sqlite").read_bytes()

    result = recover_historical_attachment(
        client, config, root, post_id=115152, media_id=115305,
        evidence_path=evidence_path,
    )

    assert result["mode"] == "preview" and result["writes"] == 0
    assert result["valid_distinct_inline"] == 2 and result["missing"] == 2
    assert result["lifecycle_preserved"] == "human_required"
    assert result["attempts_preserved"] == 35 and result["no_progress_preserved"] == 28
    assert WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"])) == original
    assert client.updates == []
    assert (root / "work/v2-journal/115152.json").read_text() == historical_journal
    assert (root / "work/media_registry.sqlite").read_bytes() == registry_before
    assert not (root / "work/visual_comparison_cache.json").exists()
    assert not (root / "work/telemetry.jsonl").exists()
    assert not list((root / "work").glob("v2-media-recovery/*.json"))
    assert not (root / "backups/115152/editorial.partial.json").exists()
    assert len(observed_roots) == 2 and all(value != root and not value.exists() for value in observed_roots)


def test_recovery_apply_is_idempotent_and_never_marks_media_applied(tmp_path, monkeypatch):
    root, original, _evidence, evidence_path, client, config = _fixture(tmp_path)
    _mock_validation(monkeypatch, tmp_path)

    first = recover_historical_attachment(
        client, config, root, post_id=115152, media_id=115305,
        evidence_path=evidence_path, apply=True,
    )

    state = WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"]))
    assert first["writes"] == 1 and state.state is LifecycleState.HUMAN_REQUIRED
    assert state.phase is original.phase and state.retry == original.retry
    assert [item.media_id for item in state.media.inline] == [115171, 115172, 115173, 115305]
    assert client.post["status"] == "pending" and client.post["content"]["raw"] == "<p>Conteúdo original preservado.</p>"
    assert client.post["featured_media"] == 115170 and client.post["meta"]["other"] == "preserved"
    assert client.post["meta"]["_hermes_human_reopened_from"] == "awaiting_human"
    checkpoint = json.loads((root / "backups/115152/editorial.partial.json").read_text())
    assert checkpoint["inline"]["accepted_count"] == 4
    from unicornio_editor.media.registry import connect
    with connect(root) as db:
        row = db.execute("SELECT status FROM media_usage WHERE post_id=115152 AND wordpress_media_id=115305").fetchone()
    assert row[0] == "accepted"
    journal = json.loads(next((root / "work/v2-media-recovery").glob("*.json")).read_text())
    assert journal["status"] == "complete" and journal["media_application"] == "not_attempted"
    update_count = len(client.updates)

    second = recover_historical_attachment(
        client, config, root, post_id=115152, media_id=115305,
        evidence_path=evidence_path, apply=True,
    )

    assert second["writes"] == 0 and second["already_recovered"] is True
    assert len(client.updates) == update_count


def test_recovery_apply_refuses_dry_run_and_insufficient_evidence(tmp_path, monkeypatch):
    root, _state, evidence, evidence_path, client, config = _fixture(tmp_path)
    _mock_validation(monkeypatch, tmp_path)
    config.dry_run = True
    with pytest.raises(ValueError, match="EDITOR_DRY_RUN=false"):
        recover_historical_attachment(
            client, config, root, post_id=115152, media_id=115305,
            evidence_path=evidence_path, apply=True,
        )

    config.dry_run = False
    evidence["evidence"] = {"verdict": "ambiguous", "matched": []}
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(ValueError, match="lacks required historical recovery provenance or relevance"):
        recover_historical_attachment(
            client, config, root, post_id=115152, media_id=115305,
            evidence_path=evidence_path,
        )
    assert client.updates == []


def test_recovery_rejects_evidence_not_bound_to_candidate_or_journal(tmp_path, monkeypatch):
    root, _state, _evidence, evidence_path, client, config = _fixture(tmp_path)
    _mock_validation(monkeypatch, tmp_path)
    candidate_path = root / "backups/115152/editorial.candidate.json"
    candidate = json.loads(candidate_path.read_text())
    candidate["_v2_run_id"] = "different-run"
    candidate_path.write_text(json.dumps(candidate))

    with pytest.raises(ValueError, match="historical V2 candidate and journal"):
        recover_historical_attachment(
            client, config, root, post_id=115152, media_id=115305,
            evidence_path=evidence_path,
        )
    assert client.updates == []


def test_recovery_requires_exact_candidate_urls_in_registry_evidence(tmp_path, monkeypatch):
    root, _state, evidence, evidence_path, client, config = _fixture(tmp_path)
    _mock_validation(monkeypatch, tmp_path)
    evidence["direct_image_url"] = "https://source.test/another-file.webp"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(ValueError, match="no matching accepted V2 decision"):
        recover_historical_attachment(
            client, config, root, post_id=115152, media_id=115305,
            evidence_path=evidence_path,
        )
    assert client.updates == []


@pytest.mark.parametrize("changed_field", ["status", "state", "featured", "content", "reopen_marker"])
def test_recovery_aborts_when_wordpress_changes_before_apply(tmp_path, monkeypatch, changed_field):
    root, _state, _evidence, evidence_path, client, config = _fixture(tmp_path)
    _mock_validation(monkeypatch, tmp_path)

    def mutate_post(post):
        if changed_field == "status":
            post["status"] = "awaiting_human"
        elif changed_field == "state":
            value = json.loads(post["meta"]["_hermes_work_state"])
            value["retry"]["attempts"] += 1
            post["meta"]["_hermes_work_state"] = json.dumps(value)
        elif changed_field == "featured":
            post["featured_media"] = 999
        elif changed_field == "content":
            post["content"]["raw"] = "edição humana concorrente"
        else:
            post["meta"]["_hermes_human_reopened_at"] = "2031-01-01T00:00:00+00:00"

    client.before_second_get = mutate_post

    with pytest.raises(RuntimeError, match="post changed during media recovery"):
        recover_historical_attachment(
            client, config, root, post_id=115152, media_id=115305,
            evidence_path=evidence_path, apply=True,
        )
    assert client.updates == []


def test_recovery_resumes_after_wordpress_commit_without_rewriting_state(tmp_path, monkeypatch):
    root, _original, _evidence, evidence_path, client, config = _fixture(tmp_path)
    _mock_validation(monkeypatch, tmp_path)
    import unicornio_editor.pipeline_v2.media_recovery as recovery

    real_record = recovery.record_v2_observation
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated local registry interruption")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(recovery, "record_v2_observation", fail_once)
    with pytest.raises(OSError, match="simulated local registry interruption"):
        recover_historical_attachment(
            client, config, root, post_id=115152, media_id=115305,
            evidence_path=evidence_path, apply=True,
        )

    updates_after_first_attempt = len(client.updates)
    assert updates_after_first_attempt == 1
    journal_path = next((root / "work/v2-media-recovery").glob("*.json"))
    assert json.loads(journal_path.read_text())["status"] == "wordpress_confirmed"

    result = recover_historical_attachment(
        client, config, root, post_id=115152, media_id=115305,
        evidence_path=evidence_path, apply=True,
    )

    assert result["writes"] == 1
    assert len(client.updates) == updates_after_first_attempt
    assert json.loads(journal_path.read_text())["status"] == "complete"


def _adoption_evidence(evidence, evidence_path):
    value = dict(evidence)
    for key in ("run_id", "candidate_id", "sha256", "phash", "visual_group_id", "visual_verification"):
        value.pop(key, None)
    value["operation"] = "editorial_adoption"
    evidence_path.write_text(json.dumps(value), encoding="utf-8")
    return evidence_path


def _set_article(root, client, *, title, html, focus_keyword=""):
    client.post["title"] = {"raw": title}
    path = root / "backups/115152/editorial.draft.json"
    editorial = json.loads(path.read_text())
    editorial["cleaned_html"] = html
    editorial["seo"] = {"title": title, "focus_keyword": focus_keyword}
    path.write_text(json.dumps(editorial), encoding="utf-8")


def test_adoption_requires_explicit_confirmation_and_authorization(tmp_path):
    root, _state, _evidence, evidence_path, client, config = _fixture(tmp_path)
    with pytest.raises(ValueError, match="--confirm-adoption"):
        adopt_existing_attachment(
            client, config, root, post_id=115152, media_id=115305,
            evidence_path=evidence_path, authorization_id="ticket-1",
            authorized_by="editor@example.test", authorization_reason="editor approved",
        )
    assert client.get_calls == 0 and client.updates == []


@pytest.mark.parametrize(
    ("title", "html"),
    [
        ("Naruto recebe nova arte", "<p>Naruto aparece na nova arte divulgada.</p>"),
        (
            "Três obras para acompanhar",
            "<h2>1. Naruto: nova temporada</h2><p>Naruto retorna.</p>"
            "<h2>2. Bleach: novos episódios</h2><p>Ichigo retorna.</p>",
        ),
    ],
)
def test_adoption_preview_requires_article_grounding_and_preserves_listicle_items(
    tmp_path, monkeypatch, title, html
):
    root, original, evidence, evidence_path, client, config = _fixture(tmp_path)
    evidence_path = _adoption_evidence(evidence, evidence_path)
    evidence["subject"] = "Naruto"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    _set_article(root, client, title=title, html=html, focus_keyword="Naruto")
    historical = (root / "work/v2-journal/115152.json").read_text()
    from unicornio_editor.media.registry import connect
    with connect(root) as db:
        registry_before = db.execute("SELECT COUNT(*) FROM media_decisions").fetchone()[0]
    _mock_validation(monkeypatch, tmp_path)

    result = adopt_existing_attachment(
        client, config, root, post_id=115152, media_id=115305,
        evidence_path=evidence_path, authorization_id="editorial-approval-42",
        authorized_by="editor@example.test", authorization_reason="approved for this article",
        confirm_adoption=True,
    )

    assert result["operation"] == "editorial_adoption" and result["writes"] == 0
    assert result["authorization"] == {
        "authorization_id": "editorial-approval-42",
        "authorized_by": "editor@example.test",
        "reason": "approved for this article",
    }
    assert result["lifecycle_preserved"] == "human_required"
    assert WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"])) == original
    assert client.updates == []
    assert (root / "work/v2-journal/115152.json").read_text() == historical
    with connect(root) as db:
        registry_after = db.execute("SELECT COUNT(*) FROM media_decisions").fetchone()[0]
    assert registry_after == registry_before


def test_adoption_rejects_source_relevant_only_to_operator_declared_subject(tmp_path):
    root, _original, evidence, evidence_path, client, config = _fixture(tmp_path)
    evidence_path = _adoption_evidence(evidence, evidence_path)
    evidence["subject"] = "Naruto"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    _set_article(
        root, client, title="GTA VI recebe novidades", focus_keyword="GTA VI",
        html="<p>O próximo jogo da Rockstar traz mudanças em Los Santos.</p>",
    )

    with pytest.raises(ValueError, match="not grounded in the current article"):
        adopt_existing_attachment(
            client, config, root, post_id=115152, media_id=115305,
            evidence_path=evidence_path, authorization_id="editorial-approval-44",
            authorized_by="editor@example.test", authorization_reason="approved for this article",
            confirm_adoption=True,
        )
    assert client.updates == []


def test_adoption_apply_is_disabled_without_verifiable_authorization(tmp_path, monkeypatch):
    root, original, evidence, evidence_path, client, config = _fixture(tmp_path)
    evidence_path = _adoption_evidence(evidence, evidence_path)
    _mock_validation(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="authorization can be independently verified"):
        adopt_existing_attachment(
            client, config, root, post_id=115152, media_id=115305,
            evidence_path=evidence_path, authorization_id="editorial-approval-43",
            authorized_by="editor@example.test", authorization_reason="approved for this article",
            confirm_adoption=True, apply=True,
        )
    assert client.updates == []
    assert not (root / "work/v2-media-recovery").exists()


def test_adoption_preview_redacts_signed_provenance_urls(tmp_path, monkeypatch):
    root, _original, evidence, evidence_path, client, config = _fixture(tmp_path)
    evidence_path = _adoption_evidence(evidence, evidence_path)
    evidence["subject"] = "Naruto"
    evidence["source_page_url"] = "https://source.test/article?token=secret"
    evidence["direct_image_url"] = "https://source.test/naruto.webp?sig=secret"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    _set_article(
        root, client, title="Naruto recebe nova arte", focus_keyword="Naruto",
        html="<p>Naruto aparece na nova arte.</p>",
    )
    _mock_validation(monkeypatch, tmp_path)

    result = adopt_existing_attachment(
        client, config, root, post_id=115152, media_id=115305,
        evidence_path=evidence_path, authorization_id="editorial-approval-45",
        authorized_by="editor@example.test", authorization_reason="approved for this article",
        confirm_adoption=True,
    )

    stored = json.dumps(result["proposed_state"], ensure_ascii=False)
    assert "token=secret" not in stored and "sig=secret" not in stored
    assert "source_page_url_sha256" in stored and "direct_image_url_sha256" in stored
