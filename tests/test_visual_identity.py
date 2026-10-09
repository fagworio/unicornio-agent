from __future__ import annotations

from pathlib import Path

from PIL import Image

from unicornio_editor.config import Config
from unicornio_editor.media.vision_cache import (
    get_cached_decision,
    get_cached_visual_comparison,
    set_cached_decision,
    set_cached_visual_comparison,
)
from unicornio_editor.media.visual_identity import fingerprint_path, verify_candidate_identity
from unicornio_editor.pipeline_v2.model import FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress
from unicornio_editor.media.vision_gate import VisionGateError, VisualComparison, _parse_visual_comparison, compare_visual_assets


def _image(path: Path, color: str) -> Path:
    Image.new("RGB", (32, 32), color).save(path, "WEBP")
    return path


def _config() -> Config:
    return Config(content_source="x", wordpress_url="https://example.test", wordpress_api_base="https://example.test/wp-json/wp/v2")


def test_visual_identity_round_trip_preserves_inline_and_featured_fields():
    media = MediaProgress(
        required=1,
        inline=(InlineMedia(7, "https://example.test/a.webp", 0, phash="a", sha256="b", visual_group_id="visual:b", visual_verification={"decision": "DIFFERENT"}),),
        featured=FeaturedProgress(FeaturedStatus.VALID, 8, "https://example.test/f.webp", "c", "d", "visual:c", {"decision": "INITIAL"}),
    )
    rebuilt = MediaProgress.from_dict(media.to_dict())
    assert rebuilt == media


def test_visual_comparison_cache_is_separate_from_relevance_cache(tmp_path: Path):
    set_cached_decision(tmp_path, "https://example.test/a", "subject", {"status": "MATCH"})
    set_cached_visual_comparison(tmp_path, "a" * 64, "b" * 64, {"decision": "DIFFERENT", "confidence": 1})
    assert get_cached_decision(tmp_path, "https://example.test/a", "subject")["status"] == "MATCH"
    assert get_cached_visual_comparison(tmp_path, "b" * 64, "a" * 64)["decision"] == "DIFFERENT"


def test_sha_exact_is_rejected_before_vision(tmp_path: Path):
    image = _image(tmp_path / "same.webp", "red")
    identity = fingerprint_path(image)
    result_identity, decision = verify_candidate_identity(
        image, candidate_id="new", baseline=[{**identity.to_dict(), "media_id": 4, "local_path": str(image)}], config=_config(), root=tmp_path,
    )
    assert result_identity.sha256 == identity.sha256
    assert decision.decision == "SAME_IMAGE"
    assert decision.duplicate_of == "4"


def test_missing_visual_provider_never_proves_candidate_distinct(tmp_path: Path):
    base = _image(tmp_path / "base.webp", "red")
    candidate = _image(tmp_path / "candidate.webp", "blue")
    baseline = fingerprint_path(base).to_dict() | {"media_id": 4, "local_path": str(base)}
    _, decision = verify_candidate_identity(candidate, candidate_id="new", baseline=[baseline], config=_config(), root=tmp_path)
    assert not decision.verified
    assert decision.decision == "UNVERIFIED"


def test_visual_comparison_parser_is_strict_about_decision_and_confidence():
    parsed = _parse_visual_comparison('{"decision":"SAME_ART_CROP","confidence":0.91,"reason":"crop"}', reference_id="a", candidate_id="b")
    assert parsed.duplicate
    with __import__("pytest").raises(VisionGateError):
        _parse_visual_comparison('{"decision":"MAYBE","confidence":1}', reference_id="a", candidate_id="b")


def test_crop_decision_from_gpt_rejects_candidate(monkeypatch, tmp_path: Path):
    import unicornio_editor.media.visual_identity as identity_module

    base = _image(tmp_path / "base.webp", "red")
    candidate = _image(tmp_path / "candidate.webp", "blue")
    baseline = fingerprint_path(base).to_dict() | {"media_id": 115130, "phash": "", "local_path": str(base)}
    config = Config("x", "https://example.test", "https://example.test/wp-json/wp/v2", vision_enabled=True, vision_api_key="test", visual_comparison_max_calls=1)
    monkeypatch.setattr(identity_module, "compare_visual_assets", lambda *_args, **_kwargs: VisualComparison("SAME_ART_CROP", 0.97, "115130", "115131", "same crop"))
    _, decision = verify_candidate_identity(candidate, candidate_id="115131", baseline=[baseline], config=config, root=tmp_path, comparison_budget=[0])
    assert decision.decision == "SAME_ART_CROP"
    assert decision.duplicate_of == "115130"


def test_visual_comparison_budget_is_fail_closed(monkeypatch, tmp_path: Path):
    import unicornio_editor.media.visual_identity as identity_module

    base = _image(tmp_path / "base.webp", "red")
    candidate = _image(tmp_path / "candidate.webp", "blue")
    baseline = fingerprint_path(base).to_dict() | {"media_id": 1, "phash": "", "local_path": str(base)}
    config = Config("x", "https://example.test", "https://example.test/wp-json/wp/v2", vision_enabled=True, vision_api_key="test", visual_comparison_max_calls=0)
    monkeypatch.setattr(identity_module, "compare_visual_assets", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call")))
    _, decision = verify_candidate_identity(candidate, candidate_id="2", baseline=[baseline], config=config, root=tmp_path, comparison_budget=[0])
    assert decision.decision == "UNVERIFIED"
    assert decision.reason == "visual_comparison_budget_exhausted"


def test_low_confidence_different_is_not_proof_of_distinction(monkeypatch, tmp_path: Path):
    import unicornio_editor.media.visual_identity as identity_module

    base = _image(tmp_path / "base.webp", "red")
    candidate = _image(tmp_path / "candidate.webp", "blue")
    baseline = fingerprint_path(base).to_dict() | {"media_id": 1, "phash": "", "local_path": str(base)}
    config = Config("x", "https://example.test", "https://example.test/wp-json/wp/v2", vision_enabled=True, vision_api_key="test")
    monkeypatch.setattr(identity_module, "compare_visual_assets", lambda *_args, **_kwargs: VisualComparison("DIFFERENT", 0.20, "1", "2", "unclear"))
    _, decision = verify_candidate_identity(candidate, candidate_id="2", baseline=[baseline], config=config, root=tmp_path, comparison_budget=[0])
    assert decision.decision == "UNVERIFIED"
    assert decision.reason == "visual_comparison_low_confidence"
    assert get_cached_visual_comparison(tmp_path, baseline["sha256"], fingerprint_path(candidate).sha256) is None


def test_low_and_high_each_consume_global_call_budget(monkeypatch):
    import unicornio_editor.media.vision_gate as gate

    responses = iter([
        VisualComparison("UNCERTAIN", 0.5, "a", "b", "low"),
        VisualComparison("DIFFERENT", 0.99, "a", "b", "high"),
    ])
    monkeypatch.setattr(gate, "_call_visual_comparison", lambda **_kwargs: next(responses))
    budget = [0]
    result = compare_visual_assets("data:image/png;base64,AA==", "data:image/png;base64,AA==", api_key="x", base_url="https://api.test", model="x", call_budget=budget, max_calls=2)
    assert result.decision == "DIFFERENT"
    assert budget == [2]
