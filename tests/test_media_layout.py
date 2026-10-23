from unicornio_editor.pipeline_v2.media_layout import (
    build_normal_article_layout_policy,
    validate_normal_article_layout_policy,
)
from unicornio_editor.pipeline_v2.model import MediaProgress
from unicornio_editor.media.inserter import select_normal_media_slots


def _body(paragraphs: int) -> str:
    text = "A equipe confirmou detalhes relevantes sobre o videogame e explicou o contexto da atualização aos leitores interessados."
    return "".join(f"<p>{text}</p>" for _ in range(paragraphs))


def test_layout_policy_binds_verified_targets_and_positions_to_body():
    for paragraphs, ideal, expected in ((11, 4, 4), (17, 6, 6), (10, 4, 3)):
        body = _body(paragraphs)
        policy = build_normal_article_layout_policy(
            body,
            target_ideal=ideal,
            verified_inline=(),
        )
        assert policy is not None
        assert policy["target_ideal"] == ideal
        assert policy["capacity_verified"] == expected
        assert policy["target_effective"] == expected
        assert policy["reduction_reason"] == ("layout_limited" if ideal > expected else "")
        assert all(row["body_sha256"] == policy["body_sha256"] for row in policy["placement_plan"])
        assert policy["placement_plan_sha256"]
        checked = validate_normal_article_layout_policy(
            body,
            policy,
            target_ideal=ideal,
            media=MediaProgress(required=ideal),
        )
        assert checked["valid"] is True
        if paragraphs == 11:
            assert policy["placement_plan"][-1]["anchor"] == "after_editorial_body"


def test_layout_policy_rejects_tampered_position_even_with_old_hash():
    body = _body(10)
    policy = build_normal_article_layout_policy(body, target_ideal=4, verified_inline=())
    assert policy is not None
    changed = {**policy, "placement_plan": [dict(row) for row in policy["placement_plan"]]}
    changed["placement_plan"][0]["paragraph_index"] += 1
    result = validate_normal_article_layout_policy(
        body,
        changed,
        target_ideal=4,
        media=MediaProgress(required=4),
    )
    assert result["valid"] is False
    assert result["reason"] in {"placement_plan_hash_mismatch", "layout_hash_mismatch"}


def test_verified_capacity_plan_is_separate_from_minimum_quota():
    body = _body(11)
    policy = build_normal_article_layout_policy(body, target_ideal=2, verified_inline=())
    assert policy is not None
    assert policy["target_effective"] == 2
    assert policy["capacity_verified"] == 4
    planned_slots = [item["paragraph_index"] for item in policy["placement_plan"]]
    assert len(planned_slots) == 4
    selected = select_normal_media_slots(planned_slots, 2)
    assert len(selected) == 2
    assert all(b - a >= 3 for a, b in zip(selected, selected[1:]))


def test_unbalanced_html_never_receives_layout_quota_reduction():
    body = _body(10).replace("</p>", "", 1)
    assert build_normal_article_layout_policy(body, target_ideal=4, verified_inline=()) is None


def test_closing_featured_repeat_is_allowed_only_with_verified_display_evidence():
    body = _body(11)
    featured = {
        "status": "valid", "media_id": 7,
        "media_url": "https://media.example/featured.webp",
        "sha256": "f" * 64, "phash": "f" * 16,
        "visual_group_id": "visual:featured",
        "visual_verification": {"decision": "INITIAL"},
        "alt_text": "Featured art",
        "credit_text": "Crédito da imagem: Fonte oficial",
        "width": 1280, "height": 720,
    }
    policy = build_normal_article_layout_policy(
        body,
        target_ideal=4,
        verified_inline=(),
        featured_media_id=7,
        featured_repeat_evidence=featured,
    )
    assert policy is not None
    assert policy["closing_featured_repeat"]["role"] == "closing_featured_repeat"
    assert policy["closing_featured_repeat"]["media_id"] == 7
    assert validate_normal_article_layout_policy(
        body, policy, target_ideal=4,
        media=MediaProgress.from_dict({"required": 4, "featured": featured}),
    )["valid"]

    no_credit = {**featured, "credit_text": "Fonte oficial"}
    restricted = build_normal_article_layout_policy(
        body, target_ideal=4, verified_inline=(), featured_media_id=7,
        featured_repeat_evidence=no_credit,
    )
    assert restricted is not None
    assert "closing_featured_repeat" not in restricted

    tampered = {**policy, "closing_featured_repeat": {**policy["closing_featured_repeat"], "media_id": 8}}
    validation = validate_normal_article_layout_policy(
        body, tampered, target_ideal=4,
        media=MediaProgress.from_dict({"required": 4, "featured": featured}),
    )
    assert validation["valid"] is False
