import tempfile
from pathlib import Path

from unicornio_editor.media.vision_cache import get_cached_decision, set_cached_decision
from unicornio_editor.media.vision_policy import (
    featured_vision_category,
    featured_vision_context_key,
    featured_vision_subject,
    resolve_editorial_identity,
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
    assert vision_cache_subject({"seo": {"focus_keyword": "Hades", "title": "Headline"}}) != "Hades"


def test_focus_keyword_must_be_evidenced_in_article_text():
    mismatched = {
        "title": "Headline sobre Xbox Live",
        "cleaned_html": "<p>A notícia aborda a plataforma.</p>",
        "seo": {"focus_keyword": "Hades"},
    }
    evidenced = {
        **mismatched,
        "cleaned_html": "<p>Hades recebe uma atualização.</p>",
    }
    assert resolve_editorial_identity(mismatched)["identity_source"] != "focus_keyword_evidenced"
    assert resolve_editorial_identity(evidenced)["primary_subject"].casefold() == "hades"
    assert resolve_editorial_identity(evidenced)["identity_source"] == "focus_keyword_evidenced"


def test_focus_keyword_evidence_treats_punctuation_as_word_boundaries():
    editorial = {
        "title": "Xbox Live, segundo a Procuradoria-Geral, atualiza salvaguardas.",
        "cleaned_html": "<p>Xbox Live: proteção para jogadores.</p>",
        "seo": {"focus_keyword": "Xbox Live"},
    }

    identity = resolve_editorial_identity(editorial)

    assert identity["primary_subject"].casefold() == "xbox live"
    assert identity["identity_source"] == "focus_keyword_evidenced"

    substring_only = {
        "title": "Halloween ganha novo trailer",
        "cleaned_html": "<p>A série Halloween volta em breve.</p>",
        "seo": {"focus_keyword": "Halo"},
    }
    assert resolve_editorial_identity(substring_only)["identity_source"] != "focus_keyword_evidenced"


def test_editorial_context_key_is_stable_by_phase_and_distinct_by_story():
    first = {
        "title": "Xbox Live investiga salvaguardas após denúncias",
        "editorial_type": "game",
        "seo": {"focus_keyword": "Xbox Live"},
    }
    second = {
        **first,
        "title": "Xbox Live anuncia novos recursos para assinantes",
    }
    assert featured_vision_context_key(first) == featured_vision_context_key(
        first, news_context=first["title"]
    )
    assert featured_vision_context_key(first) != featured_vision_context_key(second)


def test_editorial_context_key_decodes_wp_html_entities_consistently():
    plain = {"title": "Xbox Live & Segurança", "seo": {"focus_keyword": "Xbox Live"}}
    encoded = {"title": "Xbox Live &amp; Segurança", "seo": {"focus_keyword": "Xbox Live"}}
    tagged = {"title": "  <strong>Xbox   Live</strong> & Segurança  ", "seo": plain["seo"]}

    assert featured_vision_context_key(plain) == featured_vision_context_key(encoded)
    assert featured_vision_context_key(plain) == featured_vision_context_key(tagged)


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


def test_canonical_identity_prefers_focus_over_grammatical_headline_entity():
    editorial = {
        "title": "Procuradoria-Geral investiga Xbox Live após denúncias",
        "editorial_type": "game",
        "cleaned_html": "<p>Xbox Live atualiza salvaguardas depois das denúncias.</p>",
        "seo": {"title": "Entenda as salvaguardas", "focus_keyword": "Xbox Live"},
    }

    identity = resolve_editorial_identity(editorial)
    assert identity["primary_subject"].casefold() == "xbox live"
    assert featured_vision_subject(editorial) == identity["primary_subject"]
    assert identity["vision_category"] == "game"
    assert "Procuradoria-Geral" in identity["secondary_entities"]


def test_explicit_subject_wins_over_mismatched_focus_keyword():
    assert featured_vision_subject({
        "post_subjects": [{"subject": "Cyberpunk: Edgerunners"}],
        "seo": {"focus_keyword": "Wrong keyword", "title": "Headline"},
    }) == "Cyberpunk: Edgerunners"


def test_legacy_media_partial_with_vision_error_is_featured_only():
    assert _effective_partial_kind({
        "partial_kind": "media",
        "partial_missing": 0,
        "last_error": "imagens_visao: featured vision rejected",
    }) == "featured_vision"
