import tempfile
from pathlib import Path

from unicornio_editor.media.vision_cache import get_cached_decision, set_cached_decision
from unicornio_editor.media.vision_policy import (
    featured_vision_category,
    featured_vision_subject,
    vision_cache_subject,
)
from unicornio_editor.workflow import _effective_partial_kind


def test_featured_subject_uses_editorial_identity_priority():
    assert featured_vision_subject({
        "game_name": "  Elden Ring  ",
        "post_subjects": [{"subject": "Wrong subject"}],
        "seo": {"focus_keyword": "wrong", "title": "Headline"},
    }) == "Elden Ring"
    assert featured_vision_subject({
        "post_subjects": [{"subject": "Cyberpunk: Edgerunners"}],
        "seo": {"focus_keyword": "wrong", "title": "Headline"},
    }) == "Cyberpunk: Edgerunners"
    assert vision_cache_subject({"seo": {"focus_keyword": "Hades", "title": "Headline"}}) == "Hades"


def test_featured_category_is_editorial_type_not_universal_game_artwork():
    assert featured_vision_category({"editorial_type": "anime"}) == "anime"
    assert featured_vision_category({"post_type": "movie"}) == "movie"
    assert featured_vision_category({"editorial_type": "unknown"}) == "general_entertainment"
    assert featured_vision_category({}) == "general_entertainment"


def test_cache_does_not_treat_inconclusive_as_definitive_unrelated():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        set_cached_decision(root, "https://media.test/a.jpg", "Subject", {
            "status": "INCONCLUSIVE", "confidence": 1.0, "visual_type": "other",
        })
        cached = get_cached_decision(root, "https://media.test/a.jpg", "Subject")
        assert cached["status"] == "INCONCLUSIVE"
        assert cached["status"] != "UNRELATED"


def test_non_game_featured_subject_comes_from_post_subjects_not_seo_title():
    editorial = {
        "cleaned_html": "<p>Metroid Prime 4 recebe novidades.</p>",
        "seo": {
            "title": "Nintendo anuncia novidades importantes para os fãs",
            "focus_keyword": "Metroid Prime 4",
        },
    }
    assert featured_vision_subject(editorial) == "metroid prime 4"


def test_legacy_media_partial_with_vision_error_is_featured_only():
    assert _effective_partial_kind({
        "partial_kind": "media",
        "partial_missing": 0,
        "last_error": "imagens_visao: featured vision rejected",
    }) == "featured_vision"
