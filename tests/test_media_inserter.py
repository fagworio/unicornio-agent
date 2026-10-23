import unittest

from unicornio_editor.media.inserter import (
    MediaInsertionError,
    append_featured_credit,
    insert_media,
    minimum_normal_article_paragraphs,
    normalize_normal_article_paragraphs,
    plan_normal_media_insertions,
    remove_empty_paragraphs,
)


def _item(index=1, url="https://media.example/image.webp", alt="Imagem de jogo",
          width=1280, height=720, credit="Crédito da imagem: Autor. Imagem de jogo. Domínio público (CC0)."):
    return {
        "paragraph_index": index,
        "media_url": url,
        "alt_text": alt,
        "credit_text": credit,
        "width": width,
        "height": height,
    }


class MediaInserterTests(unittest.TestCase):
    @staticmethod
    def _paragraphs(count):
        return "".join(
            f"<p>Parágrafo {index} com contexto editorial suficiente para a notícia e seus detalhes relevantes.</p>"
            for index in range(count)
        )

    def test_planner_redistributes_normal_article_slots_from_current_html(self):
        html = self._paragraphs(13)
        plan = [_item(index=99, url=f"https://media.example/{index}.webp") for index in range(4)]
        planned = plan_normal_media_insertions(html, plan)
        slots = [item["paragraph_index"] for item in planned]
        self.assertEqual(slots, [1, 5, 8, 12])
        self.assertGreaterEqual(slots[0], 1)
        self.assertTrue(all(b - a >= 3 for a, b in zip(slots, slots[1:])))

    def test_planner_reports_structured_error_when_html_has_too_few_paragraphs(self):
        with self.assertRaises(MediaInsertionError) as error:
            plan_normal_media_insertions(
                self._paragraphs(4),
                [_item(index=index, url=f"https://media.example/{index}.webp") for index in range(4)],
            )
        self.assertEqual(error.exception.code, "insufficient_paragraph_slots")

    def test_planner_marks_final_body_boundary_explicitly(self):
        html = self._paragraphs(11)
        planned = plan_normal_media_insertions(
            html,
            [_item(index=index, url=f"https://media.example/{index}.webp") for index in range(4)],
        )
        assert [item["paragraph_index"] for item in planned] == [1, 4, 7, 10]
        assert planned[-1]["placement"] == "after_editorial_body"
        inserted = insert_media(html, planned)
        assert inserted.count('[caption id="" align="aligncenter"') == 4
        assert inserted.endswith("[/caption]")

    def test_final_body_slot_rejects_tail_markup_and_unmarked_final_index(self):
        html = self._paragraphs(11) + "<h2>Fonte final</h2>"
        with self.assertRaises(MediaInsertionError):
            plan_normal_media_insertions(
                html,
                [_item(index=index, url=f"https://media.example/tail-{index}.webp") for index in range(4)],
            )
        with self.assertRaises(MediaInsertionError):
            insert_media(
                self._paragraphs(5),
                [_item(index=4)],
            )

    def test_planner_requires_two_substantive_lead_paragraphs(self):
        html = "".join("<p>Curto.</p>" for _ in range(10)) + self._paragraphs(1)
        with self.assertRaises(MediaInsertionError) as error:
            plan_normal_media_insertions(html, [_item()])
        self.assertEqual(error.exception.code, "insufficient_substantive_paragraphs")

    def test_planner_distributes_two_four_and_six_images_after_the_lead(self):
        for image_count, paragraph_count, expected in (
            (2, 5, [1, 4]),
            (4, 11, [1, 4, 7, 10]),
            (6, 17, [1, 4, 7, 10, 13, 16]),
        ):
            plan = [_item(index=index, url=f"https://media.example/{image_count}-{index}.webp") for index in range(image_count)]
            planned = plan_normal_media_insertions(self._paragraphs(paragraph_count), plan)
            self.assertEqual([item["paragraph_index"] for item in planned], expected)

    def test_paragraph_target_aligns_two_four_six_images_with_explicit_end_slot(self):
        assert [minimum_normal_article_paragraphs(count) for count in (2, 4, 6)] == [5, 11, 17]
        assert [
            minimum_normal_article_paragraphs(count, allow_after_body=False)
            for count in (2, 4, 6)
        ] == [6, 12, 18]

    def test_empty_paragraph_cleanup_only_removes_attribute_free_blank_nodes(self):
        html = (
            "<p>Texto mantido.</p><p> &nbsp; </p><p><br /></p>"
            '<p id="anchor"><br></p><p><a href="https://example.test"></a></p>'
            "<p>[shortcode]</p>"
        )
        result = remove_empty_paragraphs(html)
        assert result["audit"] == {"removed": 2, "changed": True}
        assert result["html"] == (
            '<p>Texto mantido.</p><p id="anchor"><br></p>'
            '<p><a href="https://example.test"></a></p><p>[shortcode]</p>'
        )

    def test_normalizer_turns_nine_paragraphs_into_eleven_without_text_changes(self):
        long_body = (
            "A equipe confirmou uma atualização importante para o jogo nesta semana, "
            "com detalhes sobre personagens, sistemas e conteúdo adicional para os jogadores. "
            "O anúncio também informa que o lançamento seguirá o calendário divulgado anteriormente, "
            "sem alterar os recursos já apresentados pela desenvolvedora."
        )
        html = f"<p>{long_body}</p><p>{long_body}</p>" + "".join(
            f"<p>O parágrafo {index} explica os detalhes conhecidos até o momento.</p>"
            for index in range(1, 8)
        )
        normalized = normalize_normal_article_paragraphs(html, required_paragraphs=11)
        assert normalized["audit"]["original_paragraphs"] == 9
        assert normalized["audit"]["final_paragraphs"] == 11
        assert normalized["audit"]["original_words"] == normalized["audit"]["final_words"]
        assert normalized["audit"]["changed"] is True
        assert "<a href" not in normalized["html"]

    def test_normalizer_preserves_inline_links_and_refuses_shortcodes(self):
        linked = (
            "<p>Uma notícia importante foi confirmada pela "
            '<a href="https://example.test/fonte"><strong>fonte oficial</strong></a> '
            "e será detalhada em uma atualização posterior para os leitores.</p>"
        )
        html = linked + "".join(f"<p>Texto {index} curto.</p>" for index in range(8))
        normalized = normalize_normal_article_paragraphs(html, required_paragraphs=10)
        assert 'href="https://example.test/fonte"' in normalized["html"]
        assert "<strong>fonte oficial</strong>" in normalized["html"]

        shortcode_html = "<p>[caption]Texto que não deve ser dividido.</p>" + "".join(
            f"<p>Texto {index} curto.</p>" for index in range(8)
        )
        refused = normalize_normal_article_paragraphs(shortcode_html, required_paragraphs=10)
        assert refused["audit"]["final_paragraphs"] == 9
        assert refused["audit"]["reason"].startswith("editorial_restructure_required")

    def test_normalizer_does_not_duplicate_paragraph_attributes(self):
        body = (
            "A equipe confirmou uma atualização importante para o jogo nesta semana, "
            "com detalhes sobre personagens, sistemas e conteúdo adicional para os jogadores. "
            "O anúncio também informa que o lançamento seguirá o calendário divulgado anteriormente, "
            "sem alterar os recursos já apresentados pela desenvolvedora."
        )
        html = f'<p id="opening">{body}</p>' + self._paragraphs(5)
        result = normalize_normal_article_paragraphs(html, required_paragraphs=8)
        assert result["audit"]["final_paragraphs"] == 6
        assert result["audit"]["reason"].startswith("editorial_restructure_required")
        assert result["html"].count('id="opening"') == 1

    def test_adds_one_visible_featured_credit(self):
        credit = "Crédito da imagem: Omelete. Imagem promocional do trailer. Direitos autorais dos detentores."
        result = append_featured_credit("<p>Texto.</p><p>Continuação.</p>", credit)
        self.assertIn('<p class="image-credit">' + credit + "</p>", result)
        self.assertEqual(append_featured_credit(result, credit), result)

    def test_inserts_figure_between_paragraphs_with_credit(self):
        html = "<p>Um.</p><p>Dois.</p><p>Três.</p><p>Quatro.</p>"
        result = insert_media(html, [_item(index=1)])
        self.assertIn(
            '[caption id="" align="aligncenter" width="1280"]<img src="https://media.example/image.webp" '
            'width="1280" height="720" alt="Imagem de jogo" title="Imagem de jogo" />',
            result,
        )
        self.assertIn("Crédito da imagem: Autor. Imagem de jogo. Domínio público (CC0).[/caption]", result)
        self.assertLess(result.index('</p>[caption id="" align="aligncenter"'), result.index("<p>Três."))

    def test_rejects_insertion_inside_paragraph(self):
        with self.assertRaises(MediaInsertionError):
            insert_media("<p>Texto.</p>", [_item(index=0)])

    def test_rejects_plan_without_dimensions(self):
        with self.assertRaises(MediaInsertionError):
            insert_media("<p>Um.</p><p>Dois.</p>", [{
                "paragraph_index": 0,
                "media_url": "https://media.example/image.webp",
                "alt_text": "Imagem",
                "credit_text": "Crédito da imagem: Autor. Imagem de jogo. Domínio público (CC0).",
            }])

    def test_rejects_invalid_dimensions(self):
        with self.assertRaises(MediaInsertionError):
            insert_media("<p>Um.</p><p>Dois.</p>", [_item(index=0, width=0, height=720)])

    def test_enforces_maximum_twelve_images(self):
        plan = [_item(index=i * 3 + 1, url=f"https://media.example/{i}.webp") for i in range(13)]
        with self.assertRaises(MediaInsertionError):
            insert_media("".join("<p>Texto.</p>" for _ in range(45)), plan)

    def test_listicle_inserts_figure_immediately_after_each_h2(self):
        html = (
            "<h2>1. Melhor jogo</h2><p>Descricao do primeiro.</p>"
            "<h2>2. Segundo jogo</h2><p>Descricao do segundo.</p>"
            "<h2>3. Terceiro jogo</h2><p>Descricao do terceiro.</p>"
        )
        plan = [
            _item(
                index=i,
                url=f"https://media.example/{i}.webp",
                credit=f"Crédito da imagem: Autor. Captura {i}. Domínio público (CC0).",
            )
            for i in range(3)
        ]
        result = insert_media(html, plan, listicle=True)
        self.assertIn("<h2>1. Melhor jogo</h2>[caption id=\"\" align=\"aligncenter\"", result)
        self.assertIn("<h2>2. Segundo jogo</h2>[caption id=\"\" align=\"aligncenter\"", result)
        self.assertIn("<h2>3. Terceiro jogo</h2>[caption id=\"\" align=\"aligncenter\"", result)
        self.assertEqual(result.count("[caption id=\"\" align=\"aligncenter\""), 3)

    def test_listicle_rejects_placement_without_preceding_h2(self):
        html = "<p>Texto solto antes.</p><h2>1. Item</h2><p>Descricao.</p>"
        with self.assertRaises(MediaInsertionError):
            insert_media(html, [_item(index=0)], listicle=True)

    def test_grouped_h3_listicle_inserts_after_each_item_heading(self):
        html = (
            "<p>Introdução suficiente para contextualizar a lista.</p>"
            "<h2>Recomendações</h2>"
            "<h3>3. Primeiro jogo: descrição</h3><p>Descrição do primeiro.</p>"
            "<h3>2. Segundo jogo: descrição</h3><p>Descrição do segundo.</p>"
            "<h3>1. Terceiro jogo: descrição</h3><p>Descrição do terceiro.</p>"
        )
        plan = [_item(index=index, url=f"https://media.example/{index}.webp") for index in range(1, 4)]
        result = insert_media(html, plan, listicle=True)
        self.assertIn("<h2>Recomendações</h2><h3>3. Primeiro jogo: descrição</h3>[caption", result)
        self.assertIn("<h3>2. Segundo jogo: descrição</h3>[caption", result)
        self.assertIn("<h3>1. Terceiro jogo: descrição</h3>[caption", result)

    def test_grouped_non_numbered_h3_uses_target_heading_not_cta(self):
        html = (
            "<p>Introdução.</p><h2>Os melhores jogos</h2>"
            "<h3>Metroid Prime 4</h3><p>Descrição A.</p>"
            "<h3>Hades II</h3><p>Descrição B.</p>"
            "<h3>Confira mais novidades em nosso Portal de Notícias!</h3>"
        )
        plan = [
            _item(index=1, url="https://media.example/metroid.webp"),
            {
                **_item(index=2, url="https://media.example/hades.webp"),
                "item_title": "Hades II",
                "section_heading": "Hades II",
            },
        ]
        result = insert_media(html, plan, listicle=True)
        self.assertIn("<h3>Metroid Prime 4</h3>[caption", result)
        self.assertIn("<h3>Hades II</h3>[caption", result)
        self.assertNotIn("<h3>Confira mais novidades em nosso Portal de Notícias!</h3>[caption", result)

    def test_enforces_distance_between_images(self):
        plan = [
            _item(index=1, url="https://media.example/1.webp", alt="Um"),
            _item(index=3, url="https://media.example/2.webp", alt="Dois"),
        ]
        with self.assertRaises(MediaInsertionError):
            insert_media("".join("<p>Texto.</p>" for _ in range(8)), plan)

    def test_deduplicates_identical_credit(self):
        # O MESMO texto de credito nao se repete: remove a figura ORFA (sem img)
        # que duplica o credito de uma figura com imagem (bug da producao).
        from unicornio_editor.media.text import dedupe_credit_figures

        html = (
            '<figure class="aligncenter"><img src="https://x/a.webp" alt="A" />'
            "<figcaption>Crédito da imagem: Nintendo. Uso com crédito.</figcaption></figure>"
            '<figure class="aligncenter"><figcaption>Crédito da imagem: Nintendo. Uso com crédito.</figcaption></figure>'
            "<p>Texto.</p>"
        )
        result = dedupe_credit_figures(html)
        self.assertEqual(result.count("<figcaption>Crédito da imagem: Nintendo. Uso com crédito.</figcaption>"), 1)
        self.assertEqual(result.count('<figure class="aligncenter">'), 1)

    def test_strips_html_from_credit(self):
        # Credito com HTML (<p>...<p>) vira TEXTO PURO no figcaption.
        item = _item(index=1, credit="<p>Crédito da imagem: <strong>Autor</strong> (via Site).</p>")
        result = insert_media("<p>Um.</p><p>Dois.</p><p>Três.</p><p>Quatro.</p>", [item])
        self.assertIn("Crédito da imagem: Autor (via Site).[/caption]", result)
        self.assertNotIn("<p>Crédito", result)

    def test_adds_seo_title_to_image(self):
        # SEO sem IA: todo <img> do content ganha title (descricao/obra).
        item = _item(index=1, alt="Donkey Kong Bananza (Nintendo Switch 2)")
        result = insert_media("<p>Um.</p><p>Dois.</p><p>Três.</p><p>Quatro.</p>", [item])
        self.assertIn('title="Donkey Kong Bananza (Nintendo Switch 2)"', result)
        self.assertIn('alt="Donkey Kong Bananza (Nintendo Switch 2)"', result)

    def test_append_featured_credit_strips_html(self):
        credit = "<p>Crédito da imagem: Credit: Nintendo</p>"
        result = append_featured_credit("<p>Texto.</p><p>Cont.</p>", credit)
        self.assertIn("Credit: Nintendo", result)
        self.assertNotIn("<p>Credit", result)


if __name__ == "__main__":
    unittest.main()
