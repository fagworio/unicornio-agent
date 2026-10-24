import hashlib
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from unicornio_editor.pipeline_v2.model import FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress, MediaSearchProgress
from unicornio_editor.pipeline_v2.errors import StageError
from unicornio_editor.pipeline_v2.runtime import (
    ProductionMediaResolver,
    _adaptive_query_suffix,
    _aggregate_active_coverage,
    _media_rejection_history,
)


class Config:
    dry_run = False
    http_timeout = 1
    vision_enabled = False
    remote_url_policy = "audit"


def _article_html(paragraphs: int = 6) -> str:
    body = (
        "A equipe confirmou uma atualização importante nesta semana, com detalhes "
        "sobre personagens, sistemas e conteúdo adicional para os jogadores."
    )
    return "".join(f"<p>{body} ({index})</p>" for index in range(paragraphs))


def test_adaptive_media_planning_uses_persisted_rejection_reasons(tmp_path):
    post_dir = tmp_path / "backups" / "115152"
    post_dir.mkdir(parents=True)
    (post_dir / "editorial.validation.json").write_text(
        '{"failures":[{"detail":"duplicate_existing_frame", "invalid_media":[{"url":"https://cdn.test/old.webp"}]}]}',
        encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115152)
    suffix, reason = _adaptive_query_suffix(history, role="inline")
    assert "alternate scene" in suffix
    assert reason == "avoid_previous_duplicate"
    assert "https://cdn.test/old.webp" in history["urls"]


def test_resolver_materializes_legacy_identity_before_noop_return(monkeypatch, tmp_path):
    import unicornio_editor.pipeline_v2.runtime as runtime
    import unicornio_editor.pipeline_v2.visual_media as visual_media
    from PIL import Image

    inline_image = tmp_path / "legacy.webp"
    featured_image = tmp_path / "featured.webp"
    Image.new("RGB", (32, 32), "blue").save(inline_image, "WEBP")
    Image.new("RGB", (32, 32), "red").save(featured_image, "WEBP")
    monkeypatch.setattr(
        visual_media,
        "download_image",
        lambda url, *_args, **_kwargs: featured_image if "featured" in str(url) else inline_image,
    )
    monkeypatch.setattr(
        visual_media,
        "verify_candidate_identity",
        lambda path, **_kwargs: (
            visual_media.fingerprint_path(path),
            visual_media.VisualIdentityDecision("DIFFERENT", True, reason="fixture"),
        ),
    )
    monkeypatch.setattr(runtime, "_required_image_count_for_editorial", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [])
    monkeypatch.setattr(runtime, "editorial_subjects", lambda **_kwargs: [])

    class Client:
        def get_media(self, media_id):
            name = "featured" if media_id == 999 else "115152"
            return {"id": media_id, "source_url": f"https://wp.test/{name}.webp"}

    previous = MediaProgress(
        required=1,
        inline=(InlineMedia(115152, "https://wp.test/115152.webp", 0),),
        featured=FeaturedProgress(FeaturedStatus.VALID, 999, "https://wp.test/featured.webp"),
    )
    result = ProductionMediaResolver(Client(), Config(), tmp_path)(
        {"post_id": 115152, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(), "seo": {}},
        previous,
    )

    recovered = result.inline[0]
    assert recovered.sha256
    assert recovered.phash
    assert recovered.visual_group_id
    assert recovered.visual_verification["decision"] == "DIFFERENT"


def test_identity_recovery_does_not_mark_inline_initial_when_featured_is_unavailable(monkeypatch, tmp_path):
    import unicornio_editor.pipeline_v2.visual_media as visual_media
    from PIL import Image

    image = tmp_path / "inline.webp"
    Image.new("RGB", (32, 32), "blue").save(image, "WEBP")

    def download(url, *_args, **_kwargs):
        if "featured" in str(url):
            raise TimeoutError("featured CDN unavailable")
        return image

    monkeypatch.setattr(visual_media, "download_image", download)

    class Client:
        def get_media(self, media_id):
            name = "featured" if media_id == 115154 else "inline"
            return {"id": media_id, "source_url": f"https://wp.test/{name}.webp"}

    media = MediaProgress(
        required=1,
        inline=(InlineMedia(115153, "https://wp.test/inline.webp", 0),),
        featured=FeaturedProgress(FeaturedStatus.VALID, 115154, "https://wp.test/featured.webp"),
    )
    recovered = visual_media.materialize_media_identity(
        Client(), Config(), tmp_path, media, post_id=115234,
    )

    inline = recovered.inline[0]
    assert inline.sha256 and inline.phash and inline.visual_group_id
    assert inline.visual_verification["decision"] == "UNVERIFIED"
    assert inline.visual_verification["method"] == "baseline_identity_unavailable"
    events = [
        json.loads(line)
        for line in (tmp_path / "work" / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    failure = next(event for event in events if event["event"] == "visual_identity_recovery_failed")
    assert failure["post_id"] == 115234
    assert failure["media_id"] == 115154
    assert failure["role"] == "featured"
    assert failure["stage"] == "materialize"
    assert "featured.webp" not in json.dumps(failure)


def test_unavailable_earlier_inline_prevents_later_initial_promotion(monkeypatch, tmp_path):
    import unicornio_editor.pipeline_v2.visual_media as visual_media
    from PIL import Image

    image = tmp_path / "later.webp"
    Image.new("RGB", (32, 32), "green").save(image, "WEBP")

    def download(url, *_args, **_kwargs):
        if "115155" in str(url):
            raise TimeoutError("earlier attachment unavailable")
        return image

    monkeypatch.setattr(visual_media, "download_image", download)

    class Client:
        def get_media(self, media_id):
            return {"id": media_id, "source_url": f"https://wp.test/{media_id}.webp"}

    media = MediaProgress(
        required=2,
        inline=(
            InlineMedia(115155, "https://wp.test/115155.webp", 0),
            InlineMedia(115156, "https://wp.test/115156.webp", 1),
        ),
    )
    recovered = visual_media.materialize_media_identity(Client(), Config(), tmp_path, media, post_id=115234)

    assert recovered.inline[0].visual_verification == {}
    assert recovered.inline[1].visual_verification["decision"] == "UNVERIFIED"
    assert recovered.inline[1].visual_verification["method"] == "baseline_identity_unavailable"


def test_featured_without_url_is_resolved_from_attachment_id(monkeypatch, tmp_path):
    import unicornio_editor.pipeline_v2.visual_media as visual_media
    from PIL import Image

    image = tmp_path / "featured.webp"
    Image.new("RGB", (32, 32), "red").save(image, "WEBP")
    monkeypatch.setattr(visual_media, "download_image", lambda *_args, **_kwargs: image)

    class Client:
        def get_media(self, media_id):
            return {"id": media_id, "source_url": "https://wp.test/restored-featured.webp"}

    media = MediaProgress(
        required=1,
        featured=FeaturedProgress(FeaturedStatus.VALID, 115157, ""),
    )
    recovered = visual_media.materialize_media_identity(Client(), Config(), tmp_path, media, post_id=115234)

    assert recovered.featured.media_url == "https://wp.test/restored-featured.webp"
    assert recovered.featured.sha256
    assert recovered.featured.visual_verification["decision"] == "INITIAL"


def test_resolver_plans_reference_case_using_visual_deficit(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    planned = []

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        planned.append(batch["posts"][0]["needed"])
        query = batch["posts"][0]["query"]
        return {"posts": [{
            "reuse": [],
            "audit_candidates": [],
            "search": {
                "completed": True,
                "completion_reason": "EXHAUSTED_VERIFIED",
                "queries_attempted": 1,
                "engines_attempted": ["bing"],
                "coverage": _verified_search_coverage(query),
            },
        }]}

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 4)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Jogo"}])
    monkeypatch.setattr(runtime, "editorial_subjects", lambda **_kwargs: [])

    featured = FeaturedProgress(
        FeaturedStatus.VALID, 115170, "https://wp.test/115170.webp",
        "a" * 64, "1" * 16, "visual:" + "a" * 24,
        {"decision": "INITIAL"},
    )
    inline = (
        InlineMedia(115171, "https://wp.test/115171.webp", 0, phash="2" * 16, sha256="b" * 64,
                    visual_group_id="visual:" + "b" * 24,
                    visual_verification={"decision": "SAME_ART_CROP", "duplicate_of": "115170"}),
        InlineMedia(115172, "https://wp.test/115172.webp", 1, phash="3" * 16, sha256="c" * 64,
                    visual_group_id="visual:" + "c" * 24,
                    visual_verification={"decision": "SAME_ART_CROP", "duplicate_of": "115170"}),
        InlineMedia(115173, "https://wp.test/115173.webp", 2, phash="4" * 16, sha256="d" * 64,
                    visual_group_id="visual:" + "d" * 24,
                    visual_verification={"decision": "DIFFERENT"}),
    )
    previous = MediaProgress(required=4, inline=inline, featured=featured)

    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115152, "title": "Jogo"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(12), "seo": {}},
        previous,
    )

    assert planned
    assert set(planned) == {3}
    assert result.accepted == 3
    assert result.search.exhausted is True


def test_adaptive_history_reads_real_media_manifest(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-1"
    batch.mkdir(parents=True)
    (batch / "media.manifest.json").write_text(
        '{"posts":[{"post_id":115152,"audit_candidates":[{"direct_image_url":"https://cdn.test/same.webp","rejected_reason":"UNRELATED 0.90 other","phash":"abc"}]}]}',
        encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115152)
    assert "unrelated" in history["reasons"]
    # Old semantic judgments have no policy version and must not permanently
    # blacklist a URL/fingerprint under the current editorial policy.
    assert "https://cdn.test/same.webp" not in history["urls"]
    assert "abc" not in history["fingerprints"]


def test_current_policy_unrelated_remains_a_bounded_rejection(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-current-policy"
    batch.mkdir(parents=True)
    (batch / "media.manifest.json").write_text(
        json.dumps({"posts": [{"post_id": 115152, "audit_candidates": [{
            "direct_image_url": "https://cdn.test/current.webp",
            "rejected_reason": "UNRELATED 0.90 other",
            "sha256": "a" * 64,
            "evidence": {
                "verdict": "unrelated",
                "vision_policy_version": "featured-editorial-v4-context-scoped",
            },
        }]}]}),
        encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115152)
    assert "unrelated" in history["reasons"]
    assert "https://cdn.test/current.webp" in history["urls"]
    assert "a" * 64 in history["fingerprints"]


def test_deferred_invalid_media_does_not_blacklist_url(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-deferred"
    batch.mkdir(parents=True)
    (batch / "media.manifest.json").write_text(
        '{"posts":[{"post_id":115152,"audit_candidates":[{"disposition":"defer_retry",'
        '"detail":"source_page_unreachable","invalid_media":[{"url":"https://cdn.test/retry.webp"}]}]}]}',
        encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115152)
    assert "https://cdn.test/retry.webp" not in history["urls"]
    assert "source_page_unreachable" not in history["reasons"]


def test_derived_previous_rejection_is_not_root_evidence(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-derived"
    batch.mkdir(parents=True)
    (batch / "media.manifest.json").write_text(
        '{"posts":[{"post_id":115234,"audit_candidates":[{"rejected_reason":"previously_rejected_candidate",'
        '"url":"https://cdn.test/reconsider.webp"}]}]}', encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115234)
    assert "https://cdn.test/reconsider.webp" not in history["urls"]


def test_derived_final_disposition_without_original_evidence_is_reconsidered(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-derived-final"
    batch.mkdir(parents=True)
    (batch / "media.manifest.json").write_text(
        '{"posts":[{"post_id":115234,"audit_candidates":[{"disposition":"reject_final",'
        '"rejected_reason":"previously_rejected_candidate",'
        '"url":"https://cdn.test/reconsider-final.webp"}]}]}', encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115234)
    assert "https://cdn.test/reconsider-final.webp" not in history["urls"]


def test_resolver_contextualizes_normal_article_query(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    queries = []

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        item = batch["posts"][0]
        queries.append({"batch_id": batch["batch_id"], "query": item["query"]})
        return {"posts": [{"audit_candidates": [{
            "candidate_id": item["query"],
            "direct_image_url": "https://cdn.test/" + ("featured" if "featured" in batch["batch_id"] else "inline") + ".webp",
            "source_page_url": "https://source.test/article",
            "subject": item["subject"], "author": "Test",
            "license": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "captured_at": "2026-01-01T00:00:00Z",
            "credit_text": "Crédito da imagem: Test. Licença CC BY 4.0.",
            "alt_text": item["subject"], "valid": True,
            "evidence": {"verdict": "deterministic_match", "score": 9},
        }], "reuse": []}]}

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(runtime, "_execute_media_plan", lambda editorial, *_args, **_kwargs: ([{
        "media_id": 501 if editorial["media_plan"][0].get("is_featured") else 502,
        "media_url": editorial["media_plan"][0]["direct_image_url"],
        "featured": bool(editorial["media_plan"][0].get("is_featured")),
        "paragraph_index": 0,
    }], None, None))

    previous = MediaProgress(required=1)
    ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115234, "title": "Ex-gerentes de PR da Nintendo dizem que sketch do SNL com Elon Musk como Wario"},
        SimpleNamespace(media=previous), {"cleaned_html": _article_html(), "seo": {}}, previous,
    )
    inline_queries = [item["query"] for item in queries if "-inline-" in item["batch_id"]]
    assert any(
        "Wario" in query and "Elon Musk" in query
        and ("SNL" in query or "Saturday Night Live" in query)
        for query in inline_queries
    )


def test_resolver_preflights_normal_article_slots_before_acquisition(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Subject"}])
    monkeypatch.setattr(runtime, "editorial_subjects", lambda **_kwargs: ["Subject"])

    def unexpected_acquisition(*_args, **_kwargs):
        raise AssertionError("media acquisition must not run before structure preflight")

    monkeypatch.setattr(cli, "_resolve_media_batch", unexpected_acquisition)
    previous = MediaProgress(required=1)
    with pytest.raises(StageError, match="media_preflight:insufficient_"):
        ProductionMediaResolver(object(), Config(), tmp_path)(
            {"post_id": 115234, "title": "Subject"},
            SimpleNamespace(media=previous),
            {"cleaned_html": "<p>curto</p><p>também curto</p>", "seo": {}},
            previous,
        )


def test_resolver_preflight_counts_accepted_media_missing_from_html(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 4)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Subject"}])
    monkeypatch.setattr(runtime, "editorial_subjects", lambda **_kwargs: ["Subject"])
    monkeypatch.setattr(
        cli,
        "_resolve_media_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("media acquisition must not run when final composition cannot fit")
        ),
    )
    previous = MediaProgress(
        required=4,
        inline=tuple(
            InlineMedia(index, f"https://cdn.test/accepted-{index}.webp", index)
            for index in range(1, 4)
        ),
        featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"),
    )
    with pytest.raises(StageError, match="media_preflight:insufficient_paragraph_slots"):
        ProductionMediaResolver(object(), Config(), tmp_path)(
            {"post_id": 115234, "title": "Subject"},
            SimpleNamespace(media=previous),
            {"cleaned_html": _article_html(4), "seo": {}},
            previous,
        )


def test_derived_rejection_with_fingerprint_but_without_cause_is_reconsidered(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-fingerprint"
    batch.mkdir(parents=True)
    (batch / "media.manifest.json").write_text(
        '{"posts":[{"post_id":115234,"audit_candidates":[{"disposition":"reject_final",'
        '"rejected_reason":"previously_rejected_candidate","phash":"abcd",'
        '"url":"https://cdn.test/fingerprint.webp"}]}]}', encoding="utf-8",
    )
    assert "https://cdn.test/fingerprint.webp" not in _media_rejection_history(tmp_path, 115234)["urls"]


@pytest.mark.parametrize("cause,blocked", [
    ("timeout", False),
    ("source_page_unreachable", False),
    ("source_mismatch", True),
    ("duplicate_existing_frame", True),
])
def test_derived_history_uses_original_cause_classification(tmp_path, cause, blocked):
    batch = tmp_path / "work" / "batches" / f"batch-{cause}"
    batch.mkdir(parents=True)
    url = f"https://cdn.test/{cause}.webp"
    (batch / "media.manifest.json").write_text(
        json.dumps({"posts": [{"post_id": 115234, "audit_candidates": [{
            "disposition": "reject_final", "rejected_reason": "previously_rejected_candidate",
            "original_reason": cause, "url": url,
        }]}]}), encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115234)
    assert (url in history["urls"]) is blocked


def test_source_mismatch_without_comparison_proof_is_revalidated(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-unverified-source"
    batch.mkdir(parents=True)
    url = "https://cdn.test/unverified-source.webp"
    (batch / "media.manifest.json").write_text(
        json.dumps({"posts": [{"post_id": 115234, "audit_candidates": [{
            "disposition": "reject_final",
            "rejected_reason": "source_mismatch",
            "url": url,
            "evidence": {"verdict": "source_mismatch"},
        }]}]}), encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115234)
    assert url not in history["urls"]
    assert url in history["revalidation_urls"]


def test_reference_match_metadata_without_comparison_proof_is_revalidated(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-reference-only"
    batch.mkdir(parents=True)
    url = "https://cdn.test/reference-only.webp"
    (batch / "media.manifest.json").write_text(
        json.dumps({"posts": [{"post_id": 115234, "audit_candidates": [{
            "disposition": "reject_final",
            "rejected_reason": "source_mismatch",
            "url": url,
            "evidence": {
                "verdict": "source_mismatch",
                "verification_level": "EXACT_URL",
                "matched_image_url": url,
            },
        }]}]}), encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115234)
    assert url not in history["urls"]
    assert url in history["revalidation_urls"]


def test_invalid_media_inherits_original_definitive_cause(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-inherited-definitive"
    batch.mkdir(parents=True)
    url = "https://cdn.test/inherited-source-mismatch.webp"
    (batch / "media.manifest.json").write_text(
        json.dumps({"posts": [{"post_id": 115234, "audit_candidates": [{
            "disposition": "reject_final",
            "rejected_reason": "previously_rejected_candidate",
            "original_reason": "source_mismatch",
            "invalid_media": [{"url": url}],
        }]}]}), encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115234)
    assert url in history["urls"]
    assert "source_mismatch" in history["reasons"]
    assert "previously_rejected_candidate" not in history["reasons"]


def test_invalid_media_inherits_transient_cause_without_blacklisting(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-inherited-transient"
    batch.mkdir(parents=True)
    url = "https://cdn.test/inherited-timeout.webp"
    (batch / "media.manifest.json").write_text(
        json.dumps({"posts": [{"post_id": 115234, "audit_candidates": [{
            "disposition": "reject_final",
            "rejected_reason": "previously_rejected_candidate",
            "original_reason": "timeout",
            "invalid_media": [{"url": url}],
        }]}]}), encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115234)
    assert url not in history["urls"]
    assert "timeout" not in history["reasons"]


def test_unresolved_source_does_not_override_transient_original_cause(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-unresolved-source"
    batch.mkdir(parents=True)
    url = "https://cdn.test/unresolved-timeout.webp"
    (batch / "media.manifest.json").write_text(
        json.dumps({"posts": [{"post_id": 115234, "audit_candidates": [{
            "disposition": "reject_final",
            "rejected_reason": "previously_rejected_candidate",
            "original_reason": "timeout",
            "url": url,
            "evidence": {"verdict": "unresolved_source"},
        }]}]}), encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115234)
    assert url not in history["urls"]


def test_unresolved_source_without_original_cause_is_reconsidered(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-unresolved-only"
    batch.mkdir(parents=True)
    url = "https://cdn.test/unresolved-only.webp"
    (batch / "media.manifest.json").write_text(
        json.dumps({"posts": [{"post_id": 115234, "audit_candidates": [{
            "disposition": "reject_final",
            "url": url,
            "evidence": {"verdict": "unresolved_source"},
        }]}]}), encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115234)
    assert url not in history["urls"]


def test_unresolved_source_in_rejected_reason_is_reconsidered(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-unresolved-reason"
    batch.mkdir(parents=True)
    url = "https://cdn.test/unresolved-reason.webp"
    (batch / "media.manifest.json").write_text(
        json.dumps({"posts": [{"post_id": 115234, "audit_candidates": [{
            "disposition": "reject_final",
            "rejected_reason": "unresolved_source",
            "url": url,
        }]}]}), encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115234)
    assert url not in history["urls"]


def test_source_unreachable_in_rejected_reason_is_reconsidered(tmp_path):
    batch = tmp_path / "work" / "batches" / "batch-unreachable-reason"
    batch.mkdir(parents=True)
    url = "https://cdn.test/source-unreachable.webp"
    (batch / "media.manifest.json").write_text(
        json.dumps({"posts": [{"post_id": 115234, "audit_candidates": [{
            "disposition": "reject_final",
            "rejected_reason": "source_page_unreachable",
            "url": url,
        }]}]}), encoding="utf-8",
    )
    history = _media_rejection_history(tmp_path, 115234)
    assert url not in history["urls"]


def test_media_resolver_normalizes_library_reuse(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime
    import unicornio_editor.media.vision_policy as vision_policy

    seen = {"plans": [], "post_titles": [], "vision_editorials": []}
    original_context_key = vision_policy.featured_vision_context_key

    def capture_context(editorial, **kwargs):
        seen["vision_editorials"].append(dict(editorial))
        return original_context_key(editorial, **kwargs)

    def fake_resolve(*_args, **_kwargs):
        return {"posts": [{"reuse": [{
            "url": "https://cdn.test/existing.webp", "source": "https://source.test/page",
            "media_id": 77, "author": "Test", "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "captured_at": "2026-01-01T00:00:00Z",
            "credit_text": "Crédito da imagem: Test. Licença CC BY 4.0.",
            "alt_text": "Existing", "valid": True,
            "evidence": {"verdict": "deterministic_match", "score": 9},
        }], "audit_candidates": []}]}

    def fake_validate(_client, editorial, **kwargs):
        seen["plans"].append(editorial["media_plan"])
        seen["post_titles"].append(kwargs.get("post_title"))
        return {"valid": True, "rejected": []}

    def fake_execute(editorial, *_args, **kwargs):
        seen["featured_visual_asset"] = kwargs.get("featured_visual_asset")
        seen["post_titles"].append(kwargs.get("post_title"))
        return ([{"media_id": 77, "media_url": "https://cdn.test/existing.webp", "paragraph_index": 0, "alt_text": "existing", "credit_text": "Crédito da imagem: Existing", "featured": False}], None, None)

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "validate_media_plan", fake_validate)
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)
    monkeypatch.setattr(vision_policy, "featured_vision_context_key", capture_context)
    previous = MediaProgress(required=2)
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "Procuradoria-Geral analisa Xbox Live"},
        SimpleNamespace(media=previous),
        {
            "cleaned_html": _article_html(),
            "seo": {"title": "Entenda as medidas da plataforma", "focus_keyword": "Xbox Live"},
        },
        previous,
    )
    assert seen["plans"][0][0]["direct_image_url"] == "https://cdn.test/existing.webp"
    assert seen["plans"][0][0]["source_page_url"] == "https://source.test/page"
    assert len(seen["post_titles"]) >= 2
    assert set(seen["post_titles"]) == {"Procuradoria-Geral analisa Xbox Live"}
    assert seen["vision_editorials"]
    assert seen["vision_editorials"][0]["title"] == "Procuradoria-Geral analisa Xbox Live"
    assert seen["vision_editorials"][0]["seo"]["focus_keyword"] == "Xbox Live"
    assert seen["featured_visual_asset"] is None
    assert result.accepted == 1


def test_media_resolve_marks_local_reuse_as_not_required(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    enriched = []
    provider_queries = []

    def enrich(candidates, **kwargs):
        assert kwargs["verify"] is True
        for candidate in candidates:
            candidate["valid"] = True
            candidate["evidence"] = {"verdict": "deterministic_match", "score": 9}
            enriched.append(candidate["direct_image_url"])
        return candidates, [], []

    monkeypatch.setattr(cli, "_enriquecer_candidatos", enrich)
    monkeypatch.setattr(
        search, "search_web_images_batch",
        lambda queries, **_kwargs: provider_queries.extend(queries) or [],
    )

    monkeypatch.setattr(
        cli,
        "_reuse_from_library",
        lambda *_args, **_kwargs: [{
            "url": "https://cdn.test/reused.webp",
            "source": "https://source.test/reused",
            "media_id": 77,
            "author": "Test Author",
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "captured_at": "2026-10-01T00:00:00Z",
            "credit_text": "Crédito da imagem: Test Author. Licença CC BY 4.0.",
            "alt_text": "Test image",
        }],
    )

    result = cli._resolve_media_batch(
        object(),
        Config(),
        tmp_path,
        {
            "schema_version": 1,
            "batch_id": "local-reuse-only",
            "posts": [{
                "post_id": 115234,
                "subject": "Test",
                "query": "test query",
                "needed": 1,
                "limit": 3,
                "engine": "auto",
            }],
        },
        full=True,
    )

    progress = result["posts"][0]["search"]
    assert progress["completed"] is True
    assert progress["completion_reason"] == "NOT_REQUIRED"
    assert progress["queries_attempted"] == 0
    assert progress["coverage"]["provider_stop_reason"] == "NOT_REQUIRED"
    assert progress["coverage"]["expected_engines"] == []
    assert progress["coverage"]["planned_queries"] == 0
    assert progress["coverage"]["completed_queries"] == 0
    assert enriched == ["https://cdn.test/reused.webp"]
    assert provider_queries == []
    assert result["posts"][0]["reuse"][0]["valid"] is True


def test_legacy_library_entry_without_rights_does_not_become_reusable(tmp_path):
    import unicornio_editor.cli as cli
    from unicornio_editor.media.library_index import register

    register(
        tmp_path,
        source_url="https://cdn.test/legacy.webp",
        source_page="https://source.test/legacy",
        subject="Test Subject",
        media_id=77,
    )

    class Client:
        def get_media(self, media_id):
            return {"id": media_id}

        def search_media(self, *_args, **_kwargs):
            return []

    assert cli._reuse_from_library(Client(), tmp_path, "Test Subject", limit=1) == []


def test_media_resolver_restores_visual_verification_from_registry(monkeypatch, tmp_path):
    import unicornio_editor.media.registry as registry
    import unicornio_editor.pipeline_v2.runtime as runtime

    rows = [
        {
            "wordpress_media_id": 77,
            "canonical_url": "https://cdn.test/inline.webp",
            "sha256": "inline-sha",
            "phash": "inline-phash",
            "visual_group_id": "inline-group",
            "visual_verification": {"decision": "DIFFERENT", "method": "registry"},
            "subject": "Subject",
            "role": "inline",
            "section_slot": 0,
            "status": "applied",
        },
        {
            "wordpress_media_id": 88,
            "canonical_url": "https://cdn.test/featured.webp",
            "sha256": "featured-sha",
            "phash": "featured-phash",
            "visual_group_id": "featured-group",
            "visual_verification": {"decision": "INITIAL", "method": "registry"},
            "subject": "",
            "role": "featured",
            "section_slot": None,
            "status": "applied",
        },
    ]

    class Client:
        def get_media(self, media_id):
            return {"source_url": f"https://cdn.test/{'inline' if media_id == 77 else 'featured'}.webp"}

    monkeypatch.setattr(registry, "load_v2_media", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 1)
    previous = MediaProgress(required=1, featured=FeaturedProgress(FeaturedStatus.MISSING))
    result = ProductionMediaResolver(Client(), Config(), tmp_path)(
        {
            "post_id": 115234,
            "title": "Subject",
            "post": {
                "featured_media": 88,
                "content": {"raw": '<p>texto</p><img src="https://cdn.test/inline.webp">'},
            },
        },
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>texto editorial.</p>", "seo": {}},
        previous,
    )
    assert result.inline[0].visual_verification == {"decision": "DIFFERENT", "method": "registry"}
    assert result.featured.visual_verification == {"decision": "INITIAL", "method": "registry"}


def test_media_resolver_keeps_featured_and_inline_roles_separate(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    calls = []
    seen = {"plans": []}

    def candidate(url, subject, candidate_id):
        return {
            "candidate_id": candidate_id,
            "direct_image_url": url,
            "source_page_url": f"https://source.test/{candidate_id}",
            "subject": subject,
            "author": "Test",
            "license": "CC BY",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "captured_at": "2026-01-01T00:00:00Z",
            "credit_text": "Crédito da imagem: Test",
            "alt_text": subject,
            "valid": True,
            "evidence": {"verdict": "deterministic_match", "score": 9},
        }

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        calls.append(batch["posts"][0]["query"])
        item = batch["posts"][0]
        role = "featured" if "-featured-" in batch["batch_id"] else "inline"
        audit = []
        if role == "featured":
            audit = [
                candidate("https://cdn.test/featured.webp", item["subject"], "featured"),
                candidate("https://cdn.test/featured-alt.webp", item["subject"], "featured-alt"),
            ]
        elif "Nana" in item["query"]:
            audit = [candidate("https://cdn.test/featured-alt.webp", item["subject"], "inline")]
        return {"posts": [{"audit_candidates": audit, "reuse": []}]}

    def fake_validate(_client, editorial, **_kwargs):
        seen["plans"].append(editorial["media_plan"])
        return {"valid": True, "rejected": []}

    def fake_execute(editorial, *_args, **_kwargs):
        return ([
            {
                "media_id": 101 if item["is_featured"] else 102,
                "media_url": item["direct_image_url"],
                "paragraph_index": item["paragraph_index"],
                "alt_text": item["alt_text"],
                "credit_text": item["credit_text"],
                "featured": item["is_featured"],
            }
            for item in editorial["media_plan"]
        ], None, None)

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "validate_media_plan", fake_validate)
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [
        {"subject": "Pluto"}, {"subject": "Nana"}
    ])

    previous = MediaProgress(required=2)
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "10 melhores animes"},
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Pluto e Nana</p>", "seo": {}},
        previous,
    )

    assert [[item["is_featured"] for item in plan] for plan in seen["plans"]] == [[True], [False]]
    assert seen["plans"][0][0]["direct_image_url"] == "https://cdn.test/featured.webp"
    assert seen["plans"][1][0]["direct_image_url"] == "https://cdn.test/featured-alt.webp"
    assert any("Nana anime" in query for query in calls)
    assert result.featured.status is runtime.FeaturedStatus.VALID


def test_media_resolver_refreshes_required_on_noop_retry(tmp_path):
    inline = tuple(
        InlineMedia(
            index, f"https://cdn.test/{index}.webp", index,
            phash=f"{index:016x}", sha256=f"{index:064x}",
            visual_group_id=f"visual:{index:024x}",
            visual_verification={"decision": "DIFFERENT"},
        )
        for index in range(1, 5)
    )
    previous = MediaProgress(
        required=6,
        inline=inline,
        featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(), "seo": {}},
        previous,
    )
    assert result.required == 2
    assert result.accepted == 4
    assert result.missing == 0


def test_media_resolver_passes_baseline_phashes_to_final_executor(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    captured = {}

    candidate = {
        "candidate_id": "new-frame",
        "direct_image_url": "https://cdn.test/new.webp",
        "source_page_url": "https://source.test/page",
        "subject": "Test",
        "author": "Test",
        "license": "CC BY",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito da imagem: Test",
        "alt_text": "Test",
        "valid": True,
        "evidence": {"verdict": "deterministic_match", "score": 9},
    }

    def fake_resolve(*_args, **_kwargs):
        return {"posts": [{"reuse": [], "audit_candidates": [candidate]}]}

    def fake_validate(*_args, **_kwargs):
        return {"valid": True, "rejected": []}

    def fake_execute(_editorial, *_args, **kwargs):
        captured["previous_inline_phashes"] = kwargs["previous_inline_phashes"]
        return ([], None, None)

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(runtime, "validate_media_plan", fake_validate)
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)

    previous = MediaProgress(
        required=2,
        inline=(InlineMedia(77, "https://cdn.test/old.webp", 0, phash="baseline"),),
        featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(), "seo": {}},
        previous,
    )

    assert captured["previous_inline_phashes"] == ("baseline",)
    assert result.accepted == 1


def test_media_resolver_blocks_final_duplicate_before_upload(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime
    import unicornio_editor.workflow as workflow

    candidate = {
        "candidate_id": "new-frame",
        "direct_image_url": "https://source.test/new.jpg",
        "source_page_url": "https://source.test/page",
        "subject": "Test",
        "author": "Test",
        "license": "CC BY",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito da imagem: Test",
        "alt_text": "Test",
        "valid": True,
        "evidence": {"verdict": "deterministic_match", "score": 9},
    }

    monkeypatch.setattr(
        cli,
        "_resolve_media_batch",
        lambda *_args, **_kwargs: {"posts": [{"reuse": [], "audit_candidates": [candidate]}]},
    )
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(workflow, "_media_item_rejection", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(workflow, "download_image", lambda *_args, **_kwargs: tmp_path / "source.jpg")
    monkeypatch.setattr(workflow, "convert_to_webp", lambda *_args, **_kwargs: tmp_path / "inline.webp")
    monkeypatch.setattr(workflow, "verify_downloaded_against_source", lambda *_args, **_kwargs: (True, "verified"))
    monkeypatch.setattr(workflow, "image_dimensions", lambda *_args, **_kwargs: (1280, 720))
    monkeypatch.setattr(workflow, "image_has_transparency", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(workflow, "image_is_mostly_flat", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        "unicornio_editor.media.visual_hash.phash_from_path",
        lambda *_args, **_kwargs: "baseline",
    )
    upload = mock.Mock()
    monkeypatch.setattr(workflow, "upload_image", upload)

    previous = MediaProgress(
        required=2,
        inline=(InlineMedia(77, "https://cdn.test/old.webp", 0, phash="baseline"),),
        featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115088, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(), "seo": {}},
        previous,
    )

    upload.assert_not_called()
    assert result.accepted == 1


def test_media_search_transient_engine_failure_is_not_completed(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_PROVIDERS", "bing,yandex,google")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "_enriquecer_candidatos", lambda *_args, **_kwargs: ([], [], []))

    def fake_search(*_args, **_kwargs):
        return [{
            "query": "test query",
            "candidates": [],
            "engine_reports": {
                "bing": {"failure_kind": "rate_limited"},
                "yandex": {"failure_kind": "ok"},
                "google": {"failure_kind": "ok"},
            },
        }]

    monkeypatch.setattr(search, "search_web_images_batch", fake_search)
    result = cli._resolve_media_batch(
        object(),
        Config(),
        tmp_path,
        {
            "schema_version": 1,
            "batch_id": "test-transient-search",
            "posts": [{
                "post_id": 115088,
                "subject": "Test",
                "query": "test query",
                "needed": 1,
                "limit": 3,
                "engine": "auto",
            }],
        },
        full=True,
        allow_reuse=False,
    )

    assert result["posts"][0]["search"]["completed"] is False


def test_media_search_reaching_visual_target_is_completed_without_exhaustion(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_PROVIDERS", "bing,yandex,google")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    candidate = {
        "direct_image_url": "https://cdn.test/accepted.webp",
        "source_page_url": "https://source.test/article",
        "phash": "a" * 16,
        "evidence": {"verdict": "deterministic_match"},
    }
    monkeypatch.setattr(
        cli,
        "_enriquecer_candidatos",
        lambda candidates, **_kwargs: (list(candidates), [], []),
    )

    def fake_search(_queries, *, accept, **_kwargs):
        # The search layer stops once it has a deterministic candidate meeting
        # the requested visual capacity; another provider may remain transient.
        assert accept([candidate], "target reached query") == 1
        return [{
            "query": "target reached query",
            "candidates": [candidate],
            "engine_reports": {
                "bing": {"failure_kind": "rate_limited"},
                "yandex": {"failure_kind": "ok"},
                "google": {"failure_kind": "ok"},
            },
        }]

    monkeypatch.setattr(search, "search_web_images_batch", fake_search)
    result = cli._resolve_media_batch(
        object(),
        Config(),
        tmp_path,
        {
            "schema_version": 1,
            "batch_id": "target-reached-search",
            "posts": [{
                "post_id": 115516,
                "subject": "Test",
                "query": "target reached query",
                "needed": 1,
                "limit": 3,
                "engine": "auto",
            }],
        },
        full=True,
        allow_reuse=False,
    )

    progress = result["posts"][0]["search"]
    assert progress["completed"] is True
    assert progress["completion_reason"] == "TARGET_REACHED"
    assert progress["coverage"]["verified"] is False
    assert progress["coverage"]["capacity_reached"] is True
    assert progress["coverage"]["provider_strategy_complete"] is False
    assert progress["coverage"]["strategy_complete"] is False
    _active, _proofs, coverage_valid = _aggregate_active_coverage(
        [{"_planned_query": "target reached query", "coverage": progress["coverage"]}],
        ["target reached query"],
    )
    assert coverage_valid is False


def test_media_search_preserves_verified_exhaustion_when_candidate_quota_is_met(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_PROVIDERS", "bing,yandex")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    candidate = {
        "direct_image_url": "https://cdn.test/exhausted-approved.webp",
        "source_page_url": "https://source.test/article",
        "phash": "a" * 16,
        "valid": True,
        "evidence": {"verdict": "deterministic_match", "score": 9},
    }
    monkeypatch.setattr(
        cli,
        "_enriquecer_candidatos",
        lambda candidates, **_kwargs: (list(candidates), [], []),
    )

    def verified_search(_queries, *, accept, **_kwargs):
        assert accept([candidate], "verified query") == 1
        return [{
            "query": "verified query",
            "completion_reason": "EXHAUSTED_VERIFIED",
            "candidates": [candidate],
            "engine_reports": {
                "bing": {"failure_kind": "ok"},
                "yandex": {"failure_kind": "no_results_legitimate"},
            },
        }]

    monkeypatch.setattr(search, "search_web_images_batch", verified_search)
    result = cli._resolve_media_batch(
        object(),
        Config(),
        tmp_path,
        {
            "schema_version": 1,
            "batch_id": "verified-exhausted-with-candidate",
            "posts": [{
                "post_id": 115518,
                "subject": "Test",
                "query": "verified query",
                "needed": 1,
                "limit": 3,
                "engine": "auto",
            }],
        },
        full=True,
        allow_reuse=False,
    )

    progress = result["posts"][0]["search"]
    assert progress["completed"] is True
    assert progress["completion_reason"] == "EXHAUSTED_VERIFIED"
    assert progress["coverage"]["strategy_complete"] is True
    _active, _proofs, coverage_valid = _aggregate_active_coverage(
        [{"_planned_query": "verified query", "coverage": progress["coverage"]}],
        ["verified query"],
    )
    assert coverage_valid is True


def test_source_verification_uncertainty_does_not_erase_provider_completion(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_PROVIDERS", "bing,yandex")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    candidate = {
        "candidate_id": "deferred-1",
        "query": "a private source query",
        "direct_image_url": "https://images.example/candidate.webp?signature=secret",
        "source_page_url": "https://publisher.example/story",
        "source_verification_reason_code": "source_page_unavailable",
        "verification_stage": "source_page_fetch",
        "evidence": {
            "verdict": "unresolved_source",
            "gate": "source_verification",
            "recoverable": True,
            "reason_code": "source_page_unavailable",
        },
    }
    monkeypatch.setattr(
        cli,
        "_enriquecer_candidatos",
        lambda _candidates, **_kwargs: ([], [], [candidate]),
    )

    monkeypatch.setattr(search, "search_web_images_batch", lambda _queries, **_kwargs: [{
        "query": "verified providers, unresolved source",
        "completion_reason": "EXHAUSTED_VERIFIED",
        "candidates": [candidate],
        "engine_reports": {
            "bing": {"failure_kind": "ok"},
            "yandex": {"failure_kind": "no_results_legitimate"},
        },
    }])
    result = cli._resolve_media_batch(
        object(),
        Config(),
        tmp_path,
        {
            "schema_version": 1,
            "batch_id": "source-verification-incomplete",
            "posts": [{
                "post_id": 115600,
                "subject": "Test",
                "query": "verified providers, unresolved source",
                "needed": 2,
                "limit": 3,
                "engine": "auto",
            }],
        },
        full=True,
        allow_reuse=False,
        attempt_id="run-source-audit-1",
    )

    progress = result["posts"][0]["search"]
    coverage = progress["coverage"]
    assert progress["completed"] is False
    assert progress["completion_reason"] == "SOURCE_VERIFICATION_INCOMPLETE"
    assert coverage["provider_strategy_complete"] is True
    assert coverage["provider_queries_completed"] == 1
    assert coverage["strategy_complete"] is False
    assert coverage["candidate_verification_incomplete"] is True
    diagnostics = result["posts"][0]["candidate_diagnostics"]
    assert diagnostics[0]["attempt_id"] == "run-source-audit-1"
    assert diagnostics[0]["source_verification_category"] == "source_inaccessible"
    assert diagnostics[0]["source_verification_reason"] == "source_page_unavailable"
    assert "signature=secret" not in json.dumps(diagnostics)
    telemetry = (tmp_path / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
    assert "signature=secret" not in telemetry


def test_technical_provider_failure_still_blocks_verified_exhaustion(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_PROVIDERS", "bing,yandex")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "_enriquecer_candidatos", lambda *_args, **_kwargs: ([], [], []))
    monkeypatch.setattr(search, "search_web_images_batch", lambda _queries, **_kwargs: [{
        "query": "technical provider failure",
        "completion_reason": "EXHAUSTED_VERIFIED",
        "candidates": [],
        "engine_reports": {
            "bing": {"failure_kind": "ok"},
            "yandex": {"failure_kind": "js_required"},
        },
    }])
    result = cli._resolve_media_batch(
        object(), Config(), tmp_path,
        {"schema_version": 1, "batch_id": "provider-failure", "posts": [{
            "post_id": 115601, "subject": "Test", "query": "technical provider failure",
            "needed": 1, "limit": 3, "engine": "auto",
        }]},
        full=True, allow_reuse=False,
    )

    progress = result["posts"][0]["search"]
    assert progress["completed"] is False
    assert progress["completion_reason"] == "SEARCH_INCOMPLETE"
    assert progress["coverage"]["provider_strategy_complete"] is False
    assert progress["coverage"]["strategy_complete"] is False


def test_media_search_technical_engine_failure_is_incomplete(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_PROVIDERS", "bing,yandex,google")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "_enriquecer_candidatos", lambda *_args, **_kwargs: ([], [], []))

    def fake_search(*_args, **_kwargs):
        return [{
            "query": "technical failure",
            "candidates": [],
            "completion_reason": "EXHAUSTED_VERIFIED",
            "engine_reports": {
                "bing": {"failure_kind": "no_results_legitimate"},
                "yandex": {"failure_kind": "no_results_legitimate"},
                "google": {"failure_kind": "js_required", "error": "Google HTML requires JavaScript"},
            },
        }]

    monkeypatch.setattr(search, "search_web_images_batch", fake_search)
    result = cli._resolve_media_batch(
        object(), Config(), tmp_path,
        {"schema_version": 1, "batch_id": "technical-search", "posts": [{
            "post_id": 115090, "subject": "Test", "query": "technical failure",
            "needed": 1, "limit": 3, "engine": "auto",
        }]},
        full=True, allow_reuse=False,
    )
    progress = result["posts"][0]["search"]
    assert progress["completed"] is True
    assert progress["completion_reason"] == "EXHAUSTED_VERIFIED"
    assert progress["coverage"]["engine_reports"]["google"]["error"] == "Google HTML requires JavaScript"
    assert progress["coverage"]["fallback_available"] is False
    assert progress["coverage"]["optional_engine_failures"] == {"google": "js_required"}
    _active, _proofs, coverage_valid = _aggregate_active_coverage(
        [{"_planned_query": "technical failure", "coverage": progress["coverage"]}],
        ["technical failure"],
    )
    assert coverage_valid is True


def test_media_search_reports_unattempted_fallback_engines(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_PROVIDERS", "bing,yandex,google")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "_enriquecer_candidatos", lambda *_args, **_kwargs: ([], [], []))
    monkeypatch.setattr(
        search,
        "search_web_images_batch",
        lambda *_args, **_kwargs: [{
            "query": "fallback query",
            "candidates": [],
            "engine_reports": {"bing": {"failure_kind": "rate_limited"}},
        }],
    )

    result = cli._resolve_media_batch(
        object(), Config(), tmp_path,
        {"schema_version": 1, "batch_id": "fallback-search", "posts": [{
            "post_id": 115091, "subject": "Test", "query": "fallback query",
            "needed": 1, "limit": 3, "engine": "auto",
        }]},
        full=True, allow_reuse=False,
    )

    coverage = result["posts"][0]["search"]["coverage"]
    assert coverage["fallback_available"] is True
    assert coverage["next_fallback_engines"] == ["yandex", "google"]


def test_media_search_healthy_zero_results_is_exhausted(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    monkeypatch.setenv("EDITOR_MEDIA_SEARCH_PROVIDERS", "bing,yandex,google")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "_enriquecer_candidatos", lambda *_args, **_kwargs: ([], [], []))

    def fake_search(*_args, **_kwargs):
        return [{
            "query": "empty query",
            "candidates": [],
            "engine_reports": {
                engine: {"failure_kind": "no_results_legitimate"}
                for engine in ("bing", "yandex", "google")
            },
        }]

    monkeypatch.setattr(search, "search_web_images_batch", fake_search)
    result = cli._resolve_media_batch(
        object(),
        Config(),
        tmp_path,
        {
            "schema_version": 1,
            "batch_id": "healthy-empty-search",
            "posts": [{
                "post_id": 115089,
                "subject": "Test",
                "query": "empty query",
                "needed": 1,
                "limit": 3,
                "engine": "auto",
            }],
        },
        full=True,
        allow_reuse=False,
    )

    progress = result["posts"][0]["search"]
    assert progress["completed"] is True
    # The batch resolver records discovery completion; the production resolver
    # adds final WebP/pHash exhaustion after executing the media plan.
    assert progress["exhausted"] is False
    assert progress["completion_reason"] == "EXHAUSTED_VERIFIED"
    assert progress["coverage"]["version"] == 1
    assert progress["coverage"]["verified"] is True
    assert progress["coverage"]["strategy_complete"] is True
    assert progress["coverage"]["expected_engines"] == ["bing", "yandex"]
    assert progress["coverage"]["optional_engines"] == ["google"]
    active, _proofs, coverage_valid = _aggregate_active_coverage(
        [{"_planned_query": "empty query", "coverage": progress["coverage"]}],
        ["empty query"],
    )
    assert coverage_valid is True
    assert active["strategy_complete"] is True


def _verified_search_coverage(query):
    return {
        "version": 1,
        "query": query,
        "expected_engines": ["bing"],
        "reported_engines": ["bing"],
        "engine_results": {"bing": "no_results_legitimate"},
        "technical_failures": [],
        "planned_queries": 1,
        "completed_queries": 1,
        "strategy_complete": True,
        "verified": True,
        "accepted_visual_representatives": 0,
    }


def _search_run(query, coverage=None):
    row = {"_planned_query": query}
    if coverage is not None:
        row["coverage"] = coverage
    return row


def test_active_coverage_requires_one_matching_proof_per_executed_query():
    queries = ["redfall gameplay screenshots", "redfall key art"]
    runs = [_search_run(query, _verified_search_coverage(query)) for query in queries]

    active, proofs, complete = _aggregate_active_coverage(runs, queries)

    assert complete is True
    assert len(proofs) == len(queries)
    assert active["verified"] is True
    assert active["planned_queries"] == active["completed_queries"] == 2
    assert active["queries"] == queries


@pytest.mark.parametrize(
    ("runs", "queries"),
    [
        (
            [
                _search_run("redfall gameplay screenshots", _verified_search_coverage("redfall gameplay screenshots")),
                _search_run("redfall key art"),
            ],
            ["redfall gameplay screenshots", "redfall key art"],
        ),
        (
            [_search_run("redfall gameplay screenshots", _verified_search_coverage("unexpected query"))],
            ["redfall gameplay screenshots"],
        ),
        (
            [_search_run("redfall gameplay screenshots", {
                **_verified_search_coverage("redfall gameplay screenshots"),
                "verified": False,
                "strategy_complete": False,
                "technical_failures": ["js_required"],
                "completed_queries": 0,
            })],
            ["redfall gameplay screenshots"],
        ),
        (
            [_search_run("redfall gameplay screenshots", {
                **_verified_search_coverage("redfall gameplay screenshots"),
                "expected_engines": 4,
            })],
            ["redfall gameplay screenshots"],
        ),
        (
            [_search_run("redfall gameplay screenshots", {
                **_verified_search_coverage("redfall gameplay screenshots"),
                "reported_engines": 4,
            })],
            ["redfall gameplay screenshots"],
        ),
    ],
)
def test_active_coverage_fails_closed_for_missing_mismatched_or_technical_proof(runs, queries):
    active, _proofs, complete = _aggregate_active_coverage(runs, queries)

    assert complete is False
    assert active["verified"] is False
    assert active["strategy_complete"] is False


def test_failed_historical_attempt_does_not_block_new_complete_strategy_roundtrip():
    from unicornio_editor.checklist import run_pre_publish_checklist
    from test_checklist import ChecklistTests, FakeClient, editorial_payload, make_post

    queries = ["redfall gameplay screenshots", "redfall key art"]
    active, proofs, complete = _aggregate_active_coverage(
        [_search_run(query, _verified_search_coverage(query)) for query in queries],
        queries,
    )
    progress = MediaSearchProgress(
        completed=True,
        exhausted=True,
        completion_reason="EXHAUSTED_VERIFIED",
        coverage={
            "version": 1,
            "attempt_history": [{
                "query": "old query",
                "verified": False,
                "strategy_complete": False,
                "technical_failures": ["js_required"],
            }, *proofs],
            "active_coverage": active,
            "verified": complete,
        },
    ).to_dict()

    content = (
        "<p>Texto revisado sobre Redfall e seu lançamento.</p>"
        "<p>Mais informações sobre Redfall para o leitor.</p>"
        "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
    )
    with __import__("tempfile").TemporaryDirectory() as directory:
        from pathlib import Path

        backup_path = Path(directory) / "backups" / "42" / "snapshot.json"
        backup_path.parent.mkdir(parents=True)
        backup_path.write_text("{}")
        result = run_pre_publish_checklist(
            post=make_post(featured_media=7),
            editorial=editorial_payload(
                game_name="Redfall",
                cleaned_html=content,
                seo={
                    "title": "Redfall recebe novidades importantes",
                    "meta_description": "Redfall recebe novidades importantes, com detalhes sobre a obra, contexto, lançamento e informações confirmadas para o leitor.",
                    "focus_keyword": "Redfall",
                },
                trailer_unavailable=True,
                trailer_search_evidence={
                    "result": "official_not_found",
                    "searched_at": "2026-10-16T12:00:00+00:00",
                    "provider": "youtube",
                    "query": "Redfall trailer",
                },
            ),
            content=content,
            backup_path=backup_path,
            config=ChecklistTests().config(),
            client=FakeClient(),
            media_context={
                "enrichment_round": 1,
                "search": progress,
                "inline": {"required": 2, "accepted": []},
                "featured": {
                    "status": "valid",
                    "media_id": 7,
                    "media_url": "https://media.example/redfall-1280x720.webp",
                    "sha256": "f" * 64,
                    "phash": "f" * 16,
                    "visual_group_id": "visual:featured",
                    "visual_verification": {"decision": "INITIAL"},
                },
                "visual_identity_policy": 4,
                "visual_identity_evidence_valid": True,
            },
        )

    assert result["media_decision"]["waiver_applied"] is True


def test_serialized_incomplete_coverage_remains_blocked_by_checklist():
    from unicornio_editor.checklist import run_pre_publish_checklist
    from test_checklist import ChecklistTests, FakeClient, editorial_payload, make_post

    queries = ["redfall gameplay screenshots", "redfall key art"]
    active, _proofs, complete = _aggregate_active_coverage(
        [_search_run(queries[0], _verified_search_coverage(queries[0])), _search_run(queries[1])],
        queries,
    )
    progress = MediaSearchProgress(
        completed=False,
        exhausted=False,
        completion_reason="SEARCH_INCOMPLETE",
        coverage={"version": 1, "active_coverage": active, "verified": complete},
    ).to_dict()
    content = (
        "<p>Texto revisado sobre Redfall e seu lançamento.</p>"
        "<p>Mais informações sobre Redfall para o leitor.</p>"
        "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
    )
    with __import__("tempfile").TemporaryDirectory() as directory:
        from pathlib import Path

        backup_path = Path(directory) / "backups" / "42" / "snapshot.json"
        backup_path.parent.mkdir(parents=True)
        backup_path.write_text("{}")
        result = run_pre_publish_checklist(
            post=make_post(featured_media=7),
            editorial=editorial_payload(
                game_name="Redfall",
                cleaned_html=content,
                seo={
                    "title": "Redfall recebe novidades importantes",
                    "meta_description": "Redfall recebe novidades importantes, com detalhes sobre a obra, contexto, lançamento e informações confirmadas para o leitor.",
                    "focus_keyword": "Redfall",
                },
                trailer_unavailable=True,
                trailer_search_evidence={
                    "result": "official_not_found",
                    "searched_at": "2026-10-16T12:00:00+00:00",
                    "provider": "youtube",
                    "query": "Redfall trailer",
                },
            ),
            content=content,
            backup_path=backup_path,
            config=ChecklistTests().config(),
            client=FakeClient(),
            media_context={
                "enrichment_round": 1,
                "search": progress,
                "inline": {"required": 2, "accepted": []},
                "featured": {
                    "status": "valid",
                    "media_id": 7,
                    "media_url": "https://media.example/redfall-1280x720.webp",
                    "sha256": "f" * 64,
                    "phash": "f" * 16,
                    "visual_group_id": "visual:featured",
                    "visual_verification": {"decision": "INITIAL"},
                },
                "visual_identity_policy": 4,
                "visual_identity_evidence_valid": True,
            },
        )

    assert result["media_decision"]["waiver_applied"] is False


def test_media_resolver_promotes_normalized_existing_featured(monkeypatch, tmp_path):
    import unicornio_editor.pipeline_v2.runtime as runtime
    import unicornio_editor.pipeline_v2.visual_media as visual_media
    import unicornio_editor.workflow as workflow
    from PIL import Image

    image = tmp_path / "featured.webp"
    Image.new("RGB", (32, 32), "red").save(image, "WEBP")
    monkeypatch.setattr(visual_media, "download_image", lambda *_args, **_kwargs: image)

    calls = []

    def normalize(client, config, post, editorial, *, root):
        calls.append((post["featured_media"], root))
        return 88

    class Client:
        def get_media(self, media_id):
            return {"id": media_id, "source_url": "https://cdn.test/featured-1280x720.webp"}

    monkeypatch.setattr(workflow, "_normalize_existing_featured", normalize)
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 0)
    previous = MediaProgress(required=0, featured=FeaturedProgress(FeaturedStatus.MISSING))

    result = ProductionMediaResolver(Client(), Config(), tmp_path)(
        {
            "post_id": 115102,
            "title": "Test",
            "post": {"id": 115102, "featured_media": 7},
        },
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test.</p>", "seo": {}},
        previous,
    )

    assert calls == [(7, tmp_path)]
    assert result.featured.status is FeaturedStatus.VALID
    assert result.featured.media_id == 88
    assert result.featured.media_url.endswith("featured-1280x720.webp")


def test_media_resolver_continues_queries_after_final_rejections(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    def candidate(url, candidate_id):
        return {
            "candidate_id": candidate_id,
            "direct_image_url": url,
            "source_page_url": f"https://source.test/{candidate_id}",
            "subject": "Test",
            "author": "Test",
            "license": "CC BY",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "captured_at": "2026-01-01T00:00:00Z",
            "credit_text": "Crédito da imagem: Test",
            "alt_text": "Test",
            "valid": True,
            "evidence": {"verdict": "deterministic_match", "score": 9},
        }

    queries = []

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        query = batch["posts"][0]["query"]
        queries.append(query)
        candidates = (
            [candidate("https://cdn.test/rejected-a.webp", "a1"), candidate("https://cdn.test/rejected-b.webp", "a2")]
            if "Alpha" in query
            else [candidate("https://cdn.test/accepted.webp", "b1")]
        )
        return {"posts": [{
            "reuse": [],
            "audit_candidates": candidates,
            "search": {
                "completed": True,
                "completion_reason": "EXHAUSTED_VERIFIED",
                "queries_attempted": 1,
                "engines_attempted": ["bing"],
                "candidates_seen": len(candidates),
                **(
                    {"coverage": _verified_search_coverage(query)}
                    if "Alpha" in query else {}
                ),
            },
        }]}

    def fake_execute(editorial, *_args, **_kwargs):
        item = editorial["media_plan"][0]
        if "rejected" in item["direct_image_url"]:
            return ([{"paragraph_index": item["paragraph_index"], "status": "rejected", "detail": "source_mismatch"}], None, None)
        return ([{
            "media_id": 88,
            "media_url": item["direct_image_url"],
            "paragraph_index": item["paragraph_index"],
            "alt_text": item["alt_text"],
            "credit_text": item["credit_text"],
            "featured": False,
            "phash": "accepted",
            "status": "accepted",
        }], None, None)

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [
        {"subject": "Alpha"}, {"subject": "Beta"}
    ])
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 3)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)

    baseline = InlineMedia(77, "https://cdn.test/baseline.webp", 0, phash="baseline")
    previous = MediaProgress(
        required=3,
        inline=(baseline,),
        featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115108, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test.</p>", "seo": {}},
        previous,
    )

    assert len(queries) == 2
    assert result.accepted == 2
    assert result.inline[0].media_id == 77
    assert result.search.queries_planned == 2
    assert result.search.queries_attempted == 2
    assert result.search.queries_completed == 2
    assert result.search.completed is False
    assert result.search.completion_reason == "SEARCH_INCOMPLETE"
    assert result.search.coverage["active_coverage"]["verified"] is False
    assert result.search.exhausted is False


def test_media_resolver_uses_approved_candidate_fallback_before_target(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    candidate = {
        "candidate_id": "target-1",
        "direct_image_url": "https://cdn.test/target.webp",
        "source_page_url": "https://source.test/article",
        "subject": "Test",
        "author": "Test",
        "license": "CC BY",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito da imagem: Test",
        "alt_text": "Test",
        "valid": True,
        "evidence": {"verdict": "deterministic_match", "score": 9},
    }

    backup_candidate = {
        **candidate,
        "candidate_id": "target-2",
        "direct_image_url": "https://cdn.test/target-backup.webp",
    }
    monkeypatch.setattr(cli, "_resolve_media_batch", lambda *_args, **_kwargs: {
        "posts": [{
            "reuse": [],
            "audit_candidates": [candidate, backup_candidate],
            "search": {
                "completed": True,
                "completion_reason": "TARGET_REACHED",
                "queries_attempted": 1,
                "engines_attempted": ["bing"],
                "candidates_seen": 1,
            },
        }],
    })
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Test"}])
    monkeypatch.setattr(runtime, "editorial_subjects", lambda **_kwargs: [])
    monkeypatch.setattr(runtime, "_required_image_count_for_editorial", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    attempted_urls = []

    def execute_with_fallback(editorial, *_args, **_kwargs):
        item = editorial["media_plan"][0]
        attempted_urls.append(item["direct_image_url"])
        if item["direct_image_url"].endswith("target.webp"):
            return ([{"paragraph_index": item["paragraph_index"], "status": "rejected", "detail": "conversion_failed"}], None, None)
        return ([{
            "media_id": 115568,
            "media_url": item["direct_image_url"],
            "paragraph_index": item["paragraph_index"],
            "alt_text": "Test",
            "credit_text": "Credit",
            "featured": False,
            "phash": "a" * 16,
            "sha256": "b" * 64,
            "visual_group_id": "visual:target-backup",
            "visual_verification": {"decision": "INITIAL"},
            "status": "accepted",
        }], None, None)

    monkeypatch.setattr(runtime, "_execute_media_plan", execute_with_fallback)

    previous = MediaProgress(
        required=1,
        featured=FeaturedProgress(
            FeaturedStatus.VALID, 115567, "https://wp.test/featured.webp"
        ),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115516, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(), "seo": {}},
        previous,
    )

    assert attempted_urls == [
        "https://cdn.test/target.webp",
        "https://cdn.test/target-backup.webp",
    ]
    assert result.accepted == 1
    assert result.search.completed is True
    assert result.search.completion_reason == "TARGET_REACHED"
    assert result.search.coverage["active_coverage"]["verified"] is False


def test_media_resolver_preserves_target_reached_after_mixed_completed_queries(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    calls = []
    candidate = {
        "candidate_id": "target-after-exhaustion",
        "direct_image_url": "https://cdn.test/target-after-exhaustion.webp",
        "source_page_url": "https://source.test/article",
        "subject": "Beta",
        "author": "Test",
        "license": "CC BY",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito da imagem: Test",
        "alt_text": "Test",
        "valid": True,
        "evidence": {"verdict": "deterministic_match", "score": 9},
    }

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        query = batch["posts"][0]["query"]
        calls.append(query)
        if "Alpha" in query:
            search_result = {
                "completed": True,
                "completion_reason": "EXHAUSTED_VERIFIED",
                "coverage": _verified_search_coverage(query),
                "queries_attempted": 1,
                "engines_attempted": ["bing"],
                "candidates_seen": 0,
            }
            candidates = []
        else:
            search_result = {
                "completed": True,
                "completion_reason": "TARGET_REACHED",
                "queries_attempted": 1,
                "engines_attempted": ["bing"],
                "candidates_seen": 1,
            }
            candidates = [candidate]
        return {"posts": [{"reuse": [], "audit_candidates": candidates, "search": search_result}]}

    def fake_execute(editorial, *_args, **_kwargs):
        item = editorial["media_plan"][0]
        return ([{
            "media_id": 115568,
            "media_url": item["direct_image_url"],
            "paragraph_index": item["paragraph_index"],
            "alt_text": "Test",
            "credit_text": "Credit",
            "featured": False,
            "phash": "a" * 16,
            "sha256": "b" * 64,
            "visual_group_id": "visual:target-after-exhaustion",
            "visual_verification": {"decision": "INITIAL"},
            "status": "accepted",
        }], None, None)

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [
        {"subject": "Alpha"}, {"subject": "Beta"}
    ])
    monkeypatch.setattr(runtime, "editorial_subjects", lambda **_kwargs: [])
    monkeypatch.setattr(runtime, "_required_image_count_for_editorial", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)

    previous = MediaProgress(
        required=1,
        featured=FeaturedProgress(FeaturedStatus.VALID, 115567, "https://wp.test/featured.webp"),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115516, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(), "seo": {}},
        previous,
    )

    assert len(calls) == 2
    assert result.accepted == 1
    assert result.search.completed is True
    assert result.search.completion_reason == "TARGET_REACHED"
    assert result.search.coverage["active_coverage"]["verified"] is False
    assert result.search.exhausted is False


@pytest.mark.parametrize(
    ("provider_exhausted", "expected_completed", "expected_reason", "expected_exhausted"),
    [
        (False, False, "SEARCH_INCOMPLETE", False),
        (True, True, "EXHAUSTED_VERIFIED", True),
    ],
)
def test_media_resolver_distinguishes_discovery_target_from_verified_exhaustion(
    monkeypatch, tmp_path, provider_exhausted, expected_completed, expected_reason,
    expected_exhausted,
):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    candidate = {
        "candidate_id": "rejected-target",
        "direct_image_url": "https://cdn.test/rejected-target.webp",
        "source_page_url": "https://source.test/article",
        "subject": "Test",
        "author": "Test",
        "license": "CC BY",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito da imagem: Test",
        "alt_text": "Test",
        "valid": True,
        "evidence": {"verdict": "deterministic_match", "score": 9},
    }

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        query = batch["posts"][0]["query"]
        search_result = {
            "completed": True,
            "completion_reason": (
                "EXHAUSTED_VERIFIED" if provider_exhausted else "TARGET_REACHED"
            ),
            "queries_attempted": 1,
            "engines_attempted": ["bing"],
            "candidates_seen": 1,
        }
        if provider_exhausted:
            search_result["coverage"] = _verified_search_coverage(query)
        return {"posts": [{
            "reuse": [],
            "audit_candidates": [candidate],
            "search": search_result,
        }]}

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Test"}])
    monkeypatch.setattr(runtime, "editorial_subjects", lambda **_kwargs: [])
    monkeypatch.setattr(runtime, "_required_image_count_for_editorial", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(runtime, "_execute_media_plan", lambda *_args, **_kwargs: ([{
        "paragraph_index": 0,
        "status": "rejected",
        "detail": "source_mismatch",
    }], None, None))

    previous = MediaProgress(
        required=1,
        featured=FeaturedProgress(FeaturedStatus.VALID, 115567, "https://wp.test/featured.webp"),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115516, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(), "seo": {}},
        previous,
    )

    assert result.accepted == 0
    assert result.search.completed is expected_completed
    assert result.search.completion_reason == expected_reason
    assert result.search.coverage["active_coverage"]["verified"] is provider_exhausted
    assert result.search.exhausted is expected_exhausted


def test_media_resolver_varies_normal_article_queries_before_retry(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    queries = []

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        query = batch["posts"][0]["query"]
        queries.append(query)
        return {"posts": [{
            "reuse": [], "audit_candidates": [],
            "search": {"completed": True, "queries_attempted": 1, "engines_attempted": ["yandex"]},
        }]}

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Star Fox"}])
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 2)

    previous = MediaProgress(
        required=2,
        featured=FeaturedProgress(FeaturedStatus.VALID, 99, "https://cdn.test/featured.webp"),
    )
    ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 114838, "title": "Star Fox game review"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(), "seo": {}}, previous,
    )

    assert queries == [
        "Star Fox game",
        "Star Fox game gameplay screenshot",
        "Star Fox game official artwork",
        "Star Fox game character scene",
        "Star Fox game environment screenshot",
    ]


def test_featured_search_continues_after_final_rejection(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime

    def candidate(url, candidate_id):
        return {
            "candidate_id": candidate_id,
            "direct_image_url": url,
            "source_page_url": f"https://source.test/{candidate_id}",
            "subject": "Test",
            "author": "Test",
            "license": "CC BY",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "captured_at": "2026-01-01T00:00:00Z",
            "credit_text": "Crédito da imagem: Test",
            "alt_text": "Test",
            "valid": True,
            "evidence": {"verdict": "deterministic_match", "score": 9},
        }

    queries = []

    def fake_resolve(_client, _config, _root, batch, **_kwargs):
        query = batch["posts"][0]["query"]
        queries.append(query)
        return {"posts": [{
            "reuse": [],
            "audit_candidates": [candidate(
                "https://cdn.test/feature-a.webp" if len(queries) == 1 else "https://cdn.test/feature-b.webp",
                f"feature-{len(queries)}",
            )],
        }]}

    def fake_execute(editorial, *_args, **_kwargs):
        item = editorial["media_plan"][0]
        if item["direct_image_url"].endswith("feature-a.webp"):
            return ([{"paragraph_index": 0, "status": "rejected", "detail": "vision_rejected"}], None, None)
        return ([{
            "media_id": 91,
            "media_url": item["direct_image_url"],
            "paragraph_index": 0,
            "featured": True,
            "status": "accepted",
        }], 91, "Crédito")

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Test"}])
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)

    previous = MediaProgress(required=0, featured=FeaturedProgress(FeaturedStatus.MISSING))
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115102, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(3), "seo": {}},
        previous,
    )

    assert len(queries) == 2
    assert result.featured.status is FeaturedStatus.VALID
    assert result.featured.media_id == 91


def test_featured_ambiguous_candidate_is_recorded_as_retryable_in_registry(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime
    from unicornio_editor.media.registry import connect

    calls = []

    def resolve(_client, _config, _root, batch, **_kwargs):
        calls.append(batch["posts"][0]["query"])
        candidates = [{
            "candidate_id": "ambiguous-featured",
            "direct_image_url": "https://cdn.test/uncertain-key-art.webp",
            "source_page_url": "https://source.test/uncertain",
            "role": "featured",
            "subject": "Test",
            "evidence": {"verdict": "ambiguous", "gate": "visual_match"},
        }] if len(calls) == 1 else []
        return {"posts": [{"reuse": [], "audit_candidates": candidates}]}

    monkeypatch.setattr(cli, "_resolve_media_batch", resolve)
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Test"}])
    previous = MediaProgress(required=0, featured=FeaturedProgress(FeaturedStatus.MISSING))

    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115286, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(3), "seo": {}},
        previous,
    )

    assert len(calls) == 2
    assert result.featured.status is FeaturedStatus.MISSING
    with connect(tmp_path) as db:
        decisions = db.execute(
            "SELECT role, disposition, reason_code FROM media_decisions "
            "WHERE post_id=? AND candidate_id=?",
            (115286, "ambiguous-featured"),
        ).fetchall()
    assert [tuple(row) for row in decisions] == [
        ("featured", "defer_retry", "ambiguous")
    ]


def test_featured_resolver_persists_actual_execution_and_vision_decision(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime
    import unicornio_editor.media.registry as registry
    from unicornio_editor.media.registry import connect
    from unicornio_editor.media.vision_policy import (
        VISION_POLICY_VERSION,
        featured_vision_context_key,
    )

    candidate = {
        "candidate_id": "featured-resolver-auto",
        "direct_image_url": "https://cdn.test/xbox-live-featured.webp",
        "source_page_url": "https://source.test/xbox-live",
        "author": "Fonte Xbox",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito da imagem: Fonte Xbox",
        "alt_text": "Arte de Xbox Live",
        "valid": True,
        "evidence": {"verdict": "deterministic_match", "score": 9},
    }
    monkeypatch.setattr(
        cli, "_resolve_media_batch",
        lambda *_args, **_kwargs: {"posts": [{"reuse": [], "audit_candidates": [candidate]}]},
    )
    monkeypatch.setattr(runtime, "_required_image_count_for_editorial", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Xbox Live"}])
    monkeypatch.setattr(runtime, "editorial_subjects", lambda **_kwargs: [{
        "subject": "Xbox Live", "heading": "Xbox Live", "item": None,
    }])
    monkeypatch.setattr(
        runtime, "validate_media_plan",
        lambda *_args, **_kwargs: {"valid": True, "rejected": []},
    )

    editorial = {
        "title": "Xbox Live anuncia novas salvaguardas",
        "game_name": "Xbox Live",
        "cleaned_html": _article_html(3),
        "seo": {"focus_keyword": "Xbox Live"},
    }
    context_key = featured_vision_context_key(editorial)

    def execute(plan_editorial, *_args, **_kwargs):
        plan = plan_editorial["media_plan"][0]
        return ([{
            "status": "accepted",
            "media_id": 115288,
            "media_url": plan["direct_image_url"],
            "paragraph_index": 0,
            "featured": True,
            "sha256": "a" * 64,
            "phash": "a" * 16,
            "visual_group_id": "visual:" + "a" * 24,
            "visual_verification": {"decision": "INITIAL"},
            "vision_evidence": {
                "status": "MATCH", "verdict": "accept", "confidence": 0.91,
                "role_fit": "unsuitable", "image_sha256": "a" * 64,
                "image_role": "featured", "policy_version": VISION_POLICY_VERSION,
                "model_identity": "vision-test-v1", "context_key": context_key,
                "subject": "Xbox Live",
            },
        }], 115288, "Crédito da imagem: Fonte Xbox")

    monkeypatch.setattr(runtime, "_execute_media_plan", execute)
    previous = MediaProgress(required=0, featured=FeaturedProgress(FeaturedStatus.MISSING))
    run_id = "resolver-run-115286"
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115286, "title": editorial["title"], "v2_run_id": run_id},
        SimpleNamespace(media=previous),
        editorial,
        previous,
    )

    assert result.featured.status is FeaturedStatus.VALID
    assert result.featured.media_id == 115288
    with connect(tmp_path) as db:
        decision = db.execute(
            "SELECT post_id, run_id, candidate_id, role, subject, disposition, evidence_reference "
            "FROM media_decisions WHERE post_id=? AND candidate_id=?",
            (115286, "featured-resolver-auto"),
        ).fetchone()
    assert decision is not None
    assert tuple(decision[:6]) == (
        115286, run_id, "featured-resolver-auto", "featured", "Xbox Live", "accepted",
    )
    evidence = json.loads(decision[6])
    assert evidence["vision_decision"]["image_sha256"] == "a" * 64
    assert evidence["vision_decision"]["context_key"] == context_key
    assert evidence["vision_decision"]["role_fit"] == "unsuitable"
    assert evidence["vision_decision"]["policy_version"] == VISION_POLICY_VERSION

    failure_root = tmp_path / "registry-write-failure"
    failure_root.mkdir()

    def fail_registry_write(*_args, **_kwargs):
        raise OSError("token=must-not-leak")

    monkeypatch.setattr(registry, "record_v2_candidate_decision", fail_registry_write)
    still_accepted = ProductionMediaResolver(object(), Config(), failure_root)(
        {"post_id": 115286, "title": editorial["title"], "v2_run_id": run_id},
        SimpleNamespace(media=previous),
        editorial,
        previous,
    )
    assert still_accepted.featured.status is FeaturedStatus.VALID
    telemetry = [
        json.loads(line)
        for line in (failure_root / "work/telemetry.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    failure_event = next(
        event for event in telemetry
        if event.get("event") == "v2_media_registry_decision_failed"
    )
    assert failure_event["exception_type"] == "OSError"
    assert "token=must-not-leak" not in json.dumps(telemetry)
    assert candidate["direct_image_url"] not in json.dumps(telemetry)


def test_featured_matching_candidate_with_incomplete_provenance_is_not_blacklisted(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime
    from unicornio_editor.media.registry import connect

    calls = []

    def resolve(_client, _config, _root, batch, **_kwargs):
        calls.append(batch["posts"][0]["query"])
        candidates = [{
            "candidate_id": "incomplete-featured",
            "direct_image_url": "https://cdn.test/incomplete-key-art.webp",
            "role": "featured",
            "subject": "Test",
            "evidence": {"verdict": "deterministic_match", "gate": "article_source"},
        }] if len(calls) == 1 else []
        return {"posts": [{"reuse": [], "audit_candidates": candidates}]}

    monkeypatch.setattr(cli, "_resolve_media_batch", resolve)
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Test"}])
    previous = MediaProgress(required=0, featured=FeaturedProgress(FeaturedStatus.MISSING))

    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115286, "title": "Test"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(3), "seo": {}},
        previous,
    )

    assert len(calls) == 2
    assert result.featured.status is FeaturedStatus.MISSING
    with connect(tmp_path) as db:
        decisions = db.execute(
            "SELECT role, disposition, reason_code FROM media_decisions "
            "WHERE post_id=? AND candidate_id=?",
            (115286, "incomplete-featured"),
        ).fetchall()
    assert [tuple(row) for row in decisions] == [
        ("featured", "defer_retry", "incomplete_provenance")
    ]


def test_vision_rejected_featured_is_not_recovered_from_wordpress(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.pipeline_v2.runtime as runtime
    import unicornio_editor.workflow as workflow

    normalize = mock.Mock(side_effect=AssertionError("rejected featured must not return"))
    search_calls = []
    monkeypatch.setattr(workflow, "_normalize_existing_featured", normalize)
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Test"}])

    def fake_resolve(*_args, **_kwargs):
        search_calls.append(True)
        return {"posts": [{"reuse": [], "audit_candidates": []}]}

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(runtime, "_execute_media_plan", lambda *_args, **_kwargs: ([], None, None))

    previous = MediaProgress(
        required=0,
        featured=FeaturedProgress(FeaturedStatus.VISION_REJECTED, None, None),
    )
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {
            "post_id": 115102,
            "title": "Test",
            "post": {"id": 115102, "featured_media": 777},
        },
        SimpleNamespace(media=previous),
        {"cleaned_html": "<p>Test.</p>", "seo": {}},
        previous,
    )

    normalize.assert_not_called()
    assert search_calls
    assert result.featured.status is FeaturedStatus.VISION_REJECTED
    assert result.featured.media_id is None


def test_media_resolver_uses_article_source_before_external_search(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.source_verify as source_verify
    import unicornio_editor.pipeline_v2.runtime as runtime

    source_candidate = {
        "candidate_id": "article-source-1",
        "direct_image_url": "https://cdn.test/article.webp",
        "source_page_url": "https://source.test/article",
        "author": "Source",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito da imagem: source.test",
        "alt_text": "Test",
        "evidence": {"verdict": "deterministic_match"},
        "valid": True,
    }

    monkeypatch.setattr(source_verify, "discover_article_source_candidates", lambda *_args, **_kwargs: [dict(source_candidate)])
    monkeypatch.setattr(cli, "_resolve_media_batch", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("external search must not run")))
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})
    monkeypatch.setattr(runtime, "_execute_media_plan", lambda editorial, *_args, **_kwargs: ([{
        "status": "accepted",
        "media_id": 44,
        "media_url": "https://cdn.test/article.webp",
        "paragraph_index": editorial["media_plan"][0]["paragraph_index"],
        "alt_text": "Test",
        "credit_text": "Crédito",
        "featured": False,
        "sha256": "b" * 64,
        "phash": "2" * 16,
        "visual_group_id": "visual:" + "b" * 24,
        "visual_verification": {"decision": "DIFFERENT"},
    }], None, None))
    monkeypatch.setattr(runtime, "required_image_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Test"}])

    previous = MediaProgress(required=1, featured=FeaturedProgress(
        FeaturedStatus.VALID,
        99,
        "https://cdn.test/featured.webp",
        "a" * 64,
        "1" * 16,
        "visual:" + "a" * 24,
        {"decision": "INITIAL"},
    ))
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "Test", "original_link": "https://source.test/article"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(3), "seo": {}},
        previous,
    )
    assert result.accepted == 1
    assert result.inline[0].media_id == 44


def test_featured_missing_searches_article_source_even_when_inline_is_complete(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.source_verify as source_verify
    import unicornio_editor.pipeline_v2.runtime as runtime

    candidate = {
        "candidate_id": "source-featured-1",
        "direct_image_url": "https://cdn.test/source-key-art.webp",
        "source_page_url": "https://source.test/article",
        "author": "Source",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito da imagem: source.test",
        "alt_text": "Test key art",
        "evidence": {"verdict": "deterministic_match"},
        "valid": True,
    }
    source_calls = []

    def discover(url, **kwargs):
        source_calls.append((url, kwargs))
        return [dict(candidate)]

    monkeypatch.setattr(source_verify, "discover_article_source_candidates", discover)
    monkeypatch.setattr(
        cli,
        "_resolve_media_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("article Fonte must be tried before external featured search")
        ),
    )
    monkeypatch.setattr(runtime, "_required_image_count_for_editorial", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(runtime, "post_subjects", lambda **_kwargs: [{"subject": "Test"}])
    monkeypatch.setattr(runtime, "validate_media_plan", lambda *_args, **_kwargs: {"valid": True, "rejected": []})

    executed = []

    def execute(editorial, *_args, **_kwargs):
        item = editorial["media_plan"][0]
        executed.append(item)
        return ([{
            "status": "accepted",
            "media_id": 99,
            "media_url": item["direct_image_url"],
            "paragraph_index": item["paragraph_index"],
            "featured": True,
            "sha256": "f" * 64,
            "phash": "f" * 16,
            "visual_group_id": "visual:" + "f" * 24,
            "visual_verification": {"decision": "DIFFERENT"},
        }], 99, "Crédito")

    monkeypatch.setattr(runtime, "_execute_media_plan", execute)
    inline = tuple(
        InlineMedia(
            media_id,
            f"https://wp.test/inline-{media_id}.webp",
            slot,
            sha256=letter * 64,
            phash=letter * 16,
            visual_group_id=f"visual:{letter * 24}",
            visual_verification={"decision": "DIFFERENT"},
        )
        for media_id, slot, letter in ((10, 0, "a"), (11, 1, "b"))
    )
    previous = MediaProgress(required=2, inline=inline, featured=FeaturedProgress(FeaturedStatus.MISSING))

    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 115286, "title": "Test", "original_link": "https://source.test/article"},
        SimpleNamespace(media=previous),
        {"cleaned_html": _article_html(3), "seo": {}},
        previous,
    )

    assert len(source_calls) == 1
    assert source_calls[0][0] == "https://source.test/article"
    assert len(result.inline) == 2
    assert result.featured.status is FeaturedStatus.VALID
    assert result.featured.media_id == 99
    assert executed[0]["role"] == "featured"
    assert executed[0]["is_featured"] is True
    events = [
        json.loads(line)
        for line in (tmp_path / "work" / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event.get("event") == "v2_featured_source_discovery"
        and event.get("outcome") == "accepted_from_source"
        for event in events
    )


def test_vision_input_failure_rejects_one_candidate_and_keeps_valid_candidate(monkeypatch, tmp_path):
    import unicornio_editor.media.vision_gate as vision_gate
    config = SimpleNamespace(
        vision_enabled=True,
        vision_api_key="test-key",
        vision_max_low=10,
        http_timeout=1,
        vision_base_url="https://vision.test/v1",
        vision_model="vision-test",
        vision_detail="low",
    )
    resolver = ProductionMediaResolver(object(), config, tmp_path)
    candidates = [
        {
            "candidate_id": "bad",
            "direct_image_url": "https://cdn.test/missing.webp",
            "subject": "Obra A",
            "role": "inline",
            "evidence": {"verdict": "ambiguous", "needs_vision": True, "score": 5},
        },
        {
            "candidate_id": "good",
            "direct_image_url": "https://cdn.test/good.webp",
            "subject": "Obra B",
            "role": "inline",
            "evidence": {"verdict": "ambiguous", "needs_vision": True, "score": 5},
        },
    ]

    def prepare(url, **_kwargs):
        if url.endswith("missing.webp"):
            raise vision_gate.VisionInputUnavailable("HTTP 404")
        return "data:image/png;base64,aGVsbG8="

    monkeypatch.setattr(vision_gate, "prepare_vision_image_input", prepare)
    monkeypatch.setattr(
        vision_gate,
        "verify_image_subject_batch",
        lambda **_kwargs: {"good": {
            "status": "MATCH", "verdict": "accept", "confidence": 0.91,
            "visual_type": "key_art", "reason": "confirmed by pixels",
        }},
    )
    resolver._resolve_ambiguous_vision(candidates, post_id=115025)

    assert candidates[0]["evidence"]["verdict"] == "vision_input_unavailable"
    assert candidates[0]["rejected_reason"] == "vision_input_unavailable"
    assert candidates[1]["evidence"]["verdict"] == "deterministic_match"
    assert candidates[1]["evidence"]["vision_decision"]["confidence"] == 0.91
    assert candidates[1]["evidence"]["vision_decision"]["visual_type"] == "key_art"
    assert candidates[1]["evidence"]["vision_image_sha256"] == hashlib.sha256(b"hello").hexdigest()
    from unicornio_editor.media.vision_cache import get_cached_decision
    from unicornio_editor.media.vision_cache import vision_model_identity
    cached = get_cached_decision(
        tmp_path, candidates[1]["direct_image_url"], "Obra B",
        image_sha256=hashlib.sha256(b"hello").hexdigest(),
        policy="subject-match-v1",
        model_identity=vision_model_identity(config.vision_base_url, config.vision_model),
    )
    assert cached["confidence"] == 0.91
    assert cached["visual_type"] == "key_art"
    telemetry = (tmp_path / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
    assert "vision_input_unavailable" in telemetry
    assert "data:image/png;base64,aGVsbG8=" not in telemetry
