import json
from types import SimpleNamespace

import pytest

from unicornio_editor.media.visual_identity import VisualIdentity, VisualIdentityDecision
from unicornio_editor.pipeline_v2.media_recovery import recover_historical_attachment
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

    class Client:
        def __init__(self):
            self.post = {
                "id": 115152, "status": "pending", "title": {"raw": "Jogo"},
                "content": {"raw": "<p>Conteúdo original preservado.</p>"},
                "featured_media": 115170, "meta": {"_hermes_work_state": json.dumps(state.to_dict()), "other": "preserved"},
            }
            self.updates = []

        def get_post(self, _post_id):
            return self.post

        def get_media(self, media_id):
            return {
                "id": media_id, "source_url": f"https://wp.test/{media_id}.webp",
                "mime_type": "image/webp", "media_details": {"width": 960, "height": 540},
                "title": {"rendered": "Jogo"}, "caption": {"rendered": "Crédito da imagem: Fonte"},
            }

        def update_post(self, _post_id, payload):
            assert set(payload) == {"meta"}
            self.updates.append(payload)
            self.post.update(payload)

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
    monkeypatch.setattr(recovery, "verify_candidate_identity", lambda *_a, **_k: (
        VisualIdentity("e" * 64, "e" * 16, "visual:" + "e" * 24),
        VisualIdentityDecision("DIFFERENT", True, reason="fixture", comparisons=({"confidence": 0.95},)),
    ))
    monkeypatch.setattr(recovery, "validate_media_plan", lambda *_a, **_k: {"valid": 1, "rejected": []})


def test_recovery_preview_is_read_only_and_preserves_human_lifecycle(tmp_path, monkeypatch):
    root, original, _evidence, evidence_path, client, config = _fixture(tmp_path)
    _mock_validation(monkeypatch, tmp_path)

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
    assert not (root / "work").exists()
    assert not (root / "backups/115152/editorial.partial.json").exists()


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
    with pytest.raises(ValueError, match="lacks the original run"):
        recover_historical_attachment(
            client, config, root, post_id=115152, media_id=115305,
            evidence_path=evidence_path,
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
