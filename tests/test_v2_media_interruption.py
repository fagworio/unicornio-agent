"""Exercise the canary path through real V2 stages and a simulated WP boundary."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from unicornio_editor.config import Config
from unicornio_editor.media.registry import connect
from unicornio_editor.media.visual_identity import VisualIdentity, VisualIdentityDecision
from unicornio_editor.pipeline_v2.model import (
    FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress, Phase, RetryInfo, WorkState,
)
from unicornio_editor.pipeline_v2.production import ProductionCandidateReader, validation_matches_candidate
from unicornio_editor.pipeline_v2.runtime import run_v2


@pytest.mark.parametrize("mismatch", [None, "run", "candidate_run", "content", "legacy"])
def test_validation_requires_matching_run_and_candidate_content(mismatch):
    candidate = {"_v2_run_id": "candidate-run", "content": "<p>Candidate</p>"}
    validation = {
        "_v2_run_id": "validation-run", "candidate_run_id": "candidate-run",
        "candidate_hash": hashlib.sha256(candidate["content"].encode()).hexdigest(),
    }
    if mismatch == "run":
        validation["_v2_run_id"] = "old-run"
    elif mismatch == "candidate_run":
        candidate["_v2_run_id"] = "other-candidate"
    elif mismatch == "content":
        candidate["content"] += "<p>Changed</p>"
    elif mismatch == "legacy":
        validation = {"passed": False, "failures": []}
    assert validation_matches_candidate(validation, candidate, "validation-run") is (mismatch is None)
    if mismatch is None:
        assert not validation_matches_candidate(validation, {"content": candidate["content"]}, "validation-run")


def canary_media():
    featured = FeaturedProgress(
        FeaturedStatus.VALID, 115170, "https://wp.test/115170.webp",
        "a" * 64, "1" * 16, "visual:a", {"decision": "INITIAL"},
    )
    inline = tuple(
        InlineMedia(
            media_id, f"https://wp.test/{media_id}.webp", slot,
            alt_text="Jogo", credit_text="Crédito da imagem: Fonte",
            sha256=digest * 64, phash=digest * 16, visual_group_id=f"visual:{digest}",
            visual_verification={"decision": decision, "duplicate_of": "115170" if slot < 2 else ""},
        )
        for media_id, slot, digest, decision in (
            (115171, 0, "b", "SAME_ART_CROP"),
            (115172, 1, "c", "SAME_ART_CROP"),
            (115173, 2, "d", "DIFFERENT"),
        )
    )
    return MediaProgress(required=4, inline=inline, featured=featured)


@pytest.mark.parametrize("failure", ["after_upload", "next_query_timeout", "complete"])
def test_canary_preserves_upload_through_stage_runner_and_persistence(tmp_path, monkeypatch, failure):
    import unicornio_editor.cli as cli
    import unicornio_editor.workflow as workflow
    import unicornio_editor.pipeline_v2.runtime as runtime
    import unicornio_editor.pipeline_v2.visual_media as visual_media
    import unicornio_editor.pipeline_v2.production_stages as stages
    import unicornio_editor.media.library_index as library
    import unicornio_editor.media.visual_identity as identity
    import unicornio_editor.media.visual_hash as visual_hash

    original = WorkState(
        phase=Phase.MEDIA, relevance_approved=True,
        retry=RetryInfo(attempts=35, no_progress=28), media=canary_media(),
    )
    paragraph = "A equipe detalhou o novo jogo, seus personagens e os sistemas de combate apresentados nesta semana."
    editorial = {"decision": "process", "cleaned_html": f"<p>{paragraph}</p>" * 13, "seo": {}}
    directory = tmp_path / "backups" / "115152"
    directory.mkdir(parents=True)
    (directory / "editorial.draft.json").write_text(json.dumps(editorial))
    stale = {"_v2_run_id": "old-run", "content": "<p>Old candidate</p>"}
    (directory / "editorial.candidate.json").write_text(json.dumps(stale))
    old_validation = {"_v2_run_id": "old-run", "passed": False, "failures": [
        {"gate": "visual_identity_verified", "invalid_media": [{"media_id": 115305}]},
    ]}
    (directory / "editorial.validation.json").write_text(json.dumps(old_validation))

    class Client:
        def __init__(self):
            self.post = {
                "id": 115152, "status": "pending", "date_gmt": "2026-10-14T00:00:00",
                "title": {"raw": "Jogo"}, "content": {"raw": "<p>Original</p>"},
                "featured_media": 115170, "meta": {"_hermes_work_state": json.dumps(original.to_dict())},
            }
            self.uploads = 0

        def list_pending(self, **_kwargs):
            return [self.post]

        def get_post(self, _post_id):
            return self.post

        def get_media(self, media_id):
            return {"id": media_id, "source_url": f"https://wp.test/{media_id}.webp"}

        def update_post(self, _post_id, payload):
            assert "content" not in payload and "featured_media" not in payload
            self.post.update(payload)

        def upload(self, *_args):
            self.uploads += 1
            return self.get_media(115305)

        def move_to_status(self, _post_id, status):
            self.post["status"] = status
            return self.post

    client = Client()
    query_calls = []
    second_run = False

    def discover(_client, _config, _root, batch, **_kwargs):
        query_calls.append(batch["posts"][0]["needed"])
        if not second_run and len(query_calls) > 1 and failure == "next_query_timeout":
            raise TimeoutError("simulated search timeout")
        candidates = []
        if second_run or len(query_calls) == 1:
            candidates = [{
                "candidate_id": "new-image", "direct_image_url": "https://source.test/new.jpg",
                "source_page_url": "https://source.test/article",
                "evidence": {"verdict": "deterministic_match"},
                **({"media_id": 115305} if second_run else {}),
            }]
        return {"posts": [{"audit_candidates": candidates, "search": {
            "completed": True, "queries_attempted": 1, "engines_attempted": ["fixture"],
        }}]}

    monkeypatch.setattr(cli, "_resolve_media_batch", discover)
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 4)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Jogo"}])
    monkeypatch.setattr(runtime, "editorial_subjects", lambda **_kwargs: [])
    monkeypatch.setattr(visual_media, "materialize_media_identity", lambda _c, _cfg, _r, media, **_kw: media)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_a, **_kw: {"rejected": []})
    monkeypatch.setattr(workflow, "_media_item_rejection", lambda *_a, **_kw: None)
    monkeypatch.setattr(workflow, "download_image", lambda *_a, **_kw: tmp_path / "image.jpg")
    monkeypatch.setattr(workflow, "convert_to_webp", lambda *_a: tmp_path / "image.webp")
    monkeypatch.setattr(workflow, "verify_downloaded_against_source", lambda **_kw: (True, "verified"))
    monkeypatch.setattr(workflow, "image_dimensions", lambda *_a: (1280, 720))
    monkeypatch.setattr(workflow, "image_has_transparency", lambda *_a: False)
    monkeypatch.setattr(visual_hash, "phash_from_path", lambda *_a: "f0" * 8)
    monkeypatch.setattr(identity, "verify_candidate_identity", lambda *_a, **_kw: (
        VisualIdentity("e" * 64, "f0" * 8, "visual:e"),
        VisualIdentityDecision("DIFFERENT", True, reason="fixture", comparisons=({"confidence": .95},)),
    ))
    monkeypatch.setattr(workflow, "upload_image", client.upload)
    if failure == "after_upload":
        def fail_index(*_a, **_kw):
            raise RuntimeError("simulated post-upload index defect")
        monkeypatch.setattr(library, "register", fail_index)
    monkeypatch.setattr(stages, "run_pre_publish_checklist", lambda **_kw: {
        "all_passed": False,
        "items": [{"name": "imagens_no_corpo", "status": "fail", "detail": "2 of 4"}],
        "media_decision": {"is_listicle": False},
    })
    config = Config("wordpress", "https://wp.test", "/wp-json/wp/v2", dry_run=False,
                    internal_links_enabled=False, v2_admission_allowlist=(115152,))
    quarantine_file = tmp_path / "config" / "v2-quarantine.json"
    quarantine_file.parent.mkdir(parents=True, exist_ok=True)
    quarantine_file.write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )
    result = run_v2(client, config, tmp_path, post_id=115152)
    assert result["failed"] == 0, result
    assert client.uploads == 1, [row.get("detail") for row in result["details"]]
    assert query_calls[0] == 3
    persisted = WorkState.from_dict(json.loads(client.post["meta"]["_hermes_work_state"]))
    assert persisted.state.value == "pending", result["details"][0].get("detail")
    assert persisted.retry.attempts == 36
    assert persisted.retry.no_progress == (0 if failure == "complete" else 28)
    assert persisted.media.valid_distinct_inline_count == 2
    assert persisted.media.effective_missing == 2
    assert [item.media_id for item in persisted.media.inline] == [115171, 115172, 115173, 115305]
    assert len({item.slot for item in persisted.media.inline}) == 4
    assert persisted.media.featured.media_id == 115170
    partial = MediaProgress.from_dict(json.loads((directory / "editorial.partial.json").read_text()))
    assert partial == persisted.media
    journal = json.loads((tmp_path / "work/v2-journal/115152.json").read_text())
    assert journal["candidate_fresh"] is (failure == "complete"), journal.get("detail")
    assert journal["validation_fresh"] is (failure == "complete")
    assert journal["media_readback_status"] == "not_attempted"
    if failure != "complete":
        assert json.loads((directory / "editorial.validation.json").read_text()) == old_validation
        assert persisted.blocker.value == ("internal_error" if failure == "after_upload" else "provider_error")
        events = [json.loads(line) for line in (tmp_path / "work/telemetry.jsonl").read_text().splitlines()]
        error = next(row for row in events if row["event"] == "v2_unexpected_error")
        assert error["run_id"] == journal["run_id"]
        assert error["post_id"] == 115152 and "traceback" in error
        assert error["error_class"] == ("internal" if failure == "after_upload" else "provider")
    else:
        validation = json.loads((directory / "editorial.validation.json").read_text())
        assert validation["_v2_run_id"] == journal["run_id"]
        events = [json.loads(line) for line in (tmp_path / "work/telemetry.jsonl").read_text().splitlines()]
        assert next(row for row in events if row["event"] == "media_post_summary")["accepted"] == 1
    with connect(tmp_path) as db:
        assert db.execute("SELECT status FROM media_usage WHERE wordpress_media_id=115305").fetchone()[0] == "accepted"
        if failure != "after_upload":
            assert db.execute("SELECT disposition FROM media_decisions WHERE candidate_id='new-image'").fetchone()[0] == "accepted"

    # An interrupted transaction recovers the same asset despite an old
    # checklist naming it. It must confirm the attachment without uploading.
    (tmp_path / "work/v2-journal/115152.json").write_text(json.dumps({
        "status": "running", "run_id": "interrupted-run", "state": original.to_dict(),
    }))
    reader = ProductionCandidateReader(client, tmp_path)
    recovered = reader._recovered_state(115152, original, {"editorial": editorial})
    assert 115305 in {item.media_id for item in recovered.media.inline}
    assert client.uploads == 1

    # Advance only the test clock to the next permitted attempt.
    second_run = True
    eligible_at = datetime.fromisoformat(persisted.retry.next_at) + timedelta(seconds=1)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=timezone.utc):
            return eligible_at.astimezone(tz)
    monkeypatch.setattr(runtime, "datetime", Clock)
    second = run_v2(client, config, tmp_path, post_id=115152)
    assert second["completed"] == 1 and second["failed"] == 0, second
    assert second["details"][0]["blocker"] == "inline_missing"
    assert client.uploads == 1
    assert client.post["content"]["raw"] == "<p>Original</p>"
    assert client.post["featured_media"] == 115170
