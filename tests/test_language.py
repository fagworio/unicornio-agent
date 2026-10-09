from unicornio_editor.language import (
    detect_language,
    editorial_language_report,
    localization_required,
)


def test_unequivocal_english_article_requires_localization():
    report = detect_language(
        "The studio announced a new release for the game. "
        "Players will receive more details about the upcoming episode and film. "
        "The latest news confirms the release date."
    )
    assert report["language"] == "en"
    assert localization_required(report)


def test_portuguese_article_does_not_trigger_localization():
    report = detect_language(
        "A desenvolvedora anunciou um novo lançamento para o jogo. "
        "Os jogadores receberão mais detalhes sobre o próximo episódio. "
        "A notícia confirma a data de lançamento."
    )
    assert report["language"] == "pt-BR"
    assert not localization_required(report)


def test_short_or_mixed_text_is_conservative():
    assert detect_language("The Last of Us") ["language"] == "uncertain"
    mixed = detect_language(
        "A equipe confirmou o lançamento do game. The studio revelou novidades. "
        "Mais detalhes serão divulgados depois."
    )
    assert mixed["language"] in {"mixed", "uncertain"}
    assert not localization_required(mixed)


def test_markup_urls_and_official_names_do_not_trigger_on_portuguese_body():
    report = editorial_language_report(
        title="The Last of Us recebe novidades no Brasil",
        content=(
            '<p>A série recebeu uma nova atualização para os fãs brasileiros.</p>'
            '<script>the studio announced a new game</script>'
            '<a href="https://example.test/the-studio">Fonte</a>'
        ),
        seo_title="The Last of Us no Brasil",
        meta_description="A série recebeu novidades e informações para o público brasileiro.",
    )
    assert report["passed"]
    assert not localization_required(report)


def test_english_body_fails_final_language_gate():
    report = editorial_language_report(
        title="New game release announced",
        content=(
            "The studio announced a new release for the game. Players will receive "
            "more details about the upcoming episode and film. The latest news "
            "confirms the release date for the show."
        ),
        seo_title="New game release announced",
        meta_description="The studio announced the latest news about the game and its release date.",
    )
    assert report["language"] == "en"
    assert not report["passed"]
    assert "content" in report["failing_fields"]


def test_official_english_names_do_not_fail_portuguese_paragraphs():
    report = editorial_language_report(
        title="The Last of Us chega ao Xbox Game Pass",
        content=(
            "<p>A série chega ao catálogo brasileiro com novidades para os fãs.</p>"
            "<p>O episódio apresenta uma nova história e mantém o foco nos personagens.</p>"
        ),
        seo_title="The Last of Us no Xbox Game Pass",
        meta_description="Confira as novidades da série no catálogo brasileiro e veja quando assistir.",
    )
    assert report["passed"]
    assert report["english_editorial_paragraphs"] == 0


def test_multiple_substantial_english_paragraphs_fail_even_when_document_is_mixed():
    report = editorial_language_report(
        title="Novidades do jogo chegam em breve",
        content=(
            "<p>A atualização chega ao Brasil e traz novos conteúdos para os jogadores.</p>"
            "<p>The studio announced a new release for the game and confirmed the launch date.</p>"
            "<p>Players will receive more details about the upcoming episode and new features.</p>"
            "<p>Os jogadores brasileiros poderão conferir as novidades em breve.</p>"
        ),
    )
    assert report["language"] in {"mixed", "pt-BR"}
    assert report["english_editorial_paragraphs"] == 2
    assert "content" in report["failing_fields"]


def test_attributed_english_quote_is_not_editorial_language_failure():
    report = editorial_language_report(
        title="Diretor anuncia novidades para a série",
        content=(
            "<p>A produção prepara uma nova temporada para o público brasileiro.</p>"
            "<blockquote><p>The studio announced a new season for the show.</p></blockquote>"
            "<p>A equipe deve divulgar mais detalhes nos próximos meses.</p>"
        ),
    )
    assert report["passed"]
    assert report["english_editorial_paragraphs"] == 0
