import hashlib
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from unicornio_editor.pipeline_v2.model import FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress
from unicornio_editor.pipeline_v2.errors import StageError
from unicornio_editor.pipeline_v2.runtime import ProductionMediaResolver, _adaptive_query_suffix, _media_rejection_history


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
        return {"posts": [{
            "reuse": [],
            "audit_candidates": [],
            "search": {"completed": True, "queries_attempted": 1, "engines_attempted": ["bing"]},
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
                "vision_policy_version": "featured-editorial-v3-multidimensional",
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
            "subject": item["subject"], "evidence": {"verdict": "deterministic_match"},
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

    seen = {"plans": []}

    def fake_resolve(*_args, **_kwargs):
        return {"posts": [{"reuse": [{"url": "https://cdn.test/existing.webp", "source": "https://source.test/page", "media_id": 77, "author": "Test", "license": "CC BY", "license_url": "https://license.test", "captured_at": "2026-01-01T00:00:00Z", "credit_text": "Crédito", "alt_text": "Existing"}], "audit_candidates": []}]}

    def fake_validate(_client, editorial, **_kwargs):
        seen["plans"].append(editorial["media_plan"])
        return {"valid": True, "rejected": []}

    def fake_execute(editorial, *_args, **_kwargs):
        seen["featured_visual_asset"] = _kwargs.get("featured_visual_asset")
        return ([{"media_id": 77, "media_url": "https://cdn.test/existing.webp", "paragraph_index": 0, "alt_text": "existing", "credit_text": "Crédito da imagem: Existing", "featured": False}], None, None)

    monkeypatch.setattr(cli, "_resolve_media_batch", fake_resolve)
    monkeypatch.setattr(runtime, "validate_media_plan", fake_validate)
    monkeypatch.setattr(runtime, "_execute_media_plan", fake_execute)
    previous = MediaProgress(required=2)
    result = ProductionMediaResolver(object(), Config(), tmp_path)(
        {"post_id": 1, "title": "Test"}, SimpleNamespace(media=previous), {"cleaned_html": _article_html(), "seo": {}}, previous
    )
    assert seen["plans"][0][0]["direct_image_url"] == "https://cdn.test/existing.webp"
    assert seen["plans"][0][0]["source_page_url"] == "https://source.test/page"
    assert seen["featured_visual_asset"] is None
    assert result.accepted == 1


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
            "license_url": "https://license.test",
            "captured_at": "2026-01-01T00:00:00Z",
            "credit_text": "Crédito da imagem: Test",
            "alt_text": subject,
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
        "license_url": "https://license.test",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito",
        "alt_text": "Test",
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
        "license_url": "https://license.test",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito",
        "alt_text": "Test",
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

    monkeypatch.setenv("EDITOR_GOOGLE_BROWSER_ENABLED", "true")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "_enriquecer_candidatos", lambda *_args, **_kwargs: ([], [], []))

    def fake_search(*_args, **_kwargs):
        return [{
            "query": "test query",
            "candidates": [],
            "engine_reports": {
                "google_browser": {"failure_kind": "ok"},
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


def test_media_search_healthy_zero_results_is_exhausted(monkeypatch, tmp_path):
    import unicornio_editor.cli as cli
    import unicornio_editor.media.search as search

    monkeypatch.setenv("EDITOR_GOOGLE_BROWSER_ENABLED", "true")
    monkeypatch.setattr(cli, "_reuse_from_library", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "_enriquecer_candidatos", lambda *_args, **_kwargs: ([], [], []))

    def fake_search(*_args, **_kwargs):
        return [{
            "query": "empty query",
            "candidates": [],
            "engine_reports": {
                engine: {"failure_kind": "no_results_legitimate"}
                for engine in ("google_browser", "bing", "yandex", "google")
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
    assert progress["completion_reason"] == "EXHAUSTED"


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
            "license_url": "https://license.test",
            "captured_at": "2026-01-01T00:00:00Z",
            "credit_text": "Crédito",
            "alt_text": "Test",
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
                "queries_attempted": 1,
                "engines_attempted": ["bing"],
                "candidates_seen": len(candidates),
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
    assert result.search.completed is True
    assert result.search.exhausted is True


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
            "license_url": "https://license.test",
            "captured_at": "2026-01-01T00:00:00Z",
            "credit_text": "Crédito",
            "alt_text": "Test",
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
        "license": "Uso com crédito",
        "license_url": "https://source.test/article",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito da imagem: source.test",
        "alt_text": "Test",
        "evidence": {"verdict": "deterministic_match"},
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
        "license": "Uso com crédito",
        "license_url": "https://source.test/article",
        "captured_at": "2026-01-01T00:00:00Z",
        "credit_text": "Crédito da imagem: source.test",
        "alt_text": "Test key art",
        "evidence": {"verdict": "deterministic_match"},
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
