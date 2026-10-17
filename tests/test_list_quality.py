import unittest
from pathlib import Path

from unicornio_editor.list_quality import (
    ListContentError,
    detect_list_format,
    extract_list_items,
    list_item_media_coverage,
    validate_list_content,
)


GOOD = """
<p>Se você ama Frieren, estas obras podem agradar.</p>
<h2>2. Maquia: o peso de viver além das pessoas que você ama</h2>
<figure class="aligncenter"><img src="maquia.webp" /></figure><p>Maquia acompanha uma história sobre tempo e perda.</p>
<h2>1. Scrapped Princess: a ciência por trás da magia</h2>
<figure class="aligncenter"><img src="scrapped.webp" /></figure><p>A série constrói regras próprias para seu mundo.</p>
"""


class ListQualityTests(unittest.TestCase):
    FIXTURE_115179 = Path(__file__).parent / "fixtures" / "post_115179_regression.html"

    def test_115179_grouped_non_numbered_h3_is_canonical_listicle_and_cta_is_excluded(self):
        html = self.FIXTURE_115179.read_text(encoding="utf-8")
        self.assertEqual(detect_list_format("5 melhores jogos para conhecer", html), 5)
        items = extract_list_items("5 melhores jogos para conhecer", html)
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0].identity, "os-melhores-jogos-para-conhecer:metroid-prime-4")
        self.assertEqual(items[-1].position, 5)
        self.assertTrue(validate_list_content("5 melhores jogos para conhecer", html)["passed"])

    def test_115179_incremental_coverage_preserves_progress_and_rejects_wrong_or_duplicate(self):
        html = self.FIXTURE_115179.read_text(encoding="utf-8")
        items = extract_list_items("5 melhores jogos para conhecer", html)

        def assets(count):
            return [
                {
                    "media_id": index + 1,
                    "media_url": f"https://media.test/{index + 1}.webp",
                    "item_identity": item.identity,
                    "item_title": item.heading,
                    "subject": item.heading,
                    "item_number": item.position,
                    "visual_verification": {"decision": "DIFFERENT"},
                }
                for index, item in enumerate(items[:count])
            ]

        self.assertEqual(len(list_item_media_coverage("5 melhores jogos para conhecer", html, {"inline": []})["missing"]), 5)
        self.assertEqual(len(list_item_media_coverage("5 melhores jogos para conhecer", html, {"inline": assets(1)})["missing"]), 4)
        self.assertEqual(len(list_item_media_coverage("5 melhores jogos para conhecer", html, {"inline": assets(4)})["missing"]), 1)
        self.assertTrue(list_item_media_coverage("5 melhores jogos para conhecer", html, {"inline": assets(5)})["passed"])
        wrong = assets(1)
        wrong[0]["item_identity"] = items[1].identity
        wrong[0]["item_title"] = items[1].heading
        wrong[0]["subject"] = items[1].heading
        self.assertFalse(list_item_media_coverage("5 melhores jogos para conhecer", html, {"inline": wrong})["passed"])
        duplicate = assets(1) * 2
        duplicate[1] = dict(duplicate[0])
        self.assertFalse(list_item_media_coverage("5 melhores jogos para conhecer", html, {"inline": duplicate})["passed"])
    def test_numero_no_titulo_e_promessa_e_nao_prova(self):
        """Bug do "N itens": o numero no titulo, sozinho, NAO faz listicle.

        Sem H2 realmente numerados o post segue a politica 2/4/6 — antes o
        titulo decidia sozinho e "40 jogos retrô e música" exigia 40 imagens
        (post travado para sempre, mesmo sendo um evento).
        """
        self.assertIsNone(
            detect_list_format("10 animes para assistir se você ama Frieren", "<p>texto</p>")
        )
        self.assertIsNone(
            detect_list_format(
                "Nostalgia Sem Wifi Festival no Parque das Árvores: 40 jogos retrô e música",
                "<p>evento</p>",
            )
        )
        self.assertIsNone(detect_list_format("Bailarina ganha novo trailer", ""))
        # Com a estrutura presente, a contagem prometida continua valendo.
        com_h2 = "<h2>1. Bleach: a saga</h2><h2>2. Pluto: o robô</h2>"
        self.assertEqual(detect_list_format("10 animes para assistir", com_h2), 10)
        self.assertEqual(detect_list_format("Os melhores jogos", com_h2), 2)

    def test_accepts_consistent_descending_list(self):
        report = validate_list_content("2 animes para assistir se você ama Frieren", GOOD)
        self.assertTrue(report["passed"])
        self.assertEqual(report["items"], 2)

    def test_rejects_wrong_count_and_missing_image_order(self):
        with self.assertRaises(ListContentError):
            validate_list_content("10 animes para assistir se você ama Frieren", GOOD)
        bad = GOOD.replace('<figure class="aligncenter"><img src="scrapped.webp" /></figure>', "<p>Texto antes da imagem.</p>")
        with self.assertRaises(ListContentError):
            validate_list_content("2 animes para assistir se você ama Frieren", bad)

    def test_rejects_article_and_unidentified_h2(self):
        with self.assertRaises(ListContentError):
            validate_list_content("2 animes para assistir se você ama Frieren", "<article>" + GOOD + "</article>")
        bad = GOOD.replace("<h2>1. Scrapped Princess: a ciência por trás da magia</h2>", "<h2>1. A ciência por trás da magia</h2>")
        with self.assertRaises(ListContentError):
            validate_list_content("2 animes para assistir se você ama Frieren", bad)
    def test_rejects_zero_image_listicle(self):
        html = (
            "<p>Intro.</p>"
            "<h2>1. Tokyo Ghoul: um horror que precisa respirar</h2>"
            "<p>Descrição do item.</p>"
            "<h2>2. Berserk: uma adaptação corajosa</h2>"
            "<p>Descrição do item 2.</p>"
        )
        with self.assertRaises(ListContentError):
            validate_list_content("2 animes para assistir se você ama Frieren", html)

    def test_recognizes_grouped_h3_items_and_requires_each_image(self):
        html = (
            "<p>Introdução.</p><h2>Recomendações</h2>"
            "<h3>5. Obra A: descrição</h3><figure><img src=\"a.webp\"></figure><p>Item A.</p>"
            "<h3>4. Obra B: descrição</h3><figure><img src=\"b.webp\"></figure><p>Item B.</p>"
            "<h3>3. Obra C: descrição</h3><figure><img src=\"c.webp\"></figure><p>Item C.</p>"
            "<h3>2. Obra D: descrição</h3><figure><img src=\"d.webp\"></figure><p>Item D.</p>"
            "<h3>1. Obra E: descrição</h3><figure><img src=\"e.webp\"></figure><p>Item E.</p>"
        )
        report = validate_list_content("5 recomendações de filmes", html)
        self.assertTrue(report["passed"])
        self.assertEqual(report["items"], 5)

    def test_mentions_five_films_without_list_structure_is_normal_article(self):
        html = (
            "<p>O festival reúne cinco filmes e discute suas influências.</p>"
            "<h2>Contexto</h2><p>O texto contextualiza as obras.</p>"
        )
        self.assertIsNone(detect_list_format("Festival anuncia cinco filmes", html))

    def test_h2_sem_numeracao_nao_e_listicle(self):
        """H2 sem numeração = artigo normal: não há contrato de lista a violar.

        Antes o título ("2 animes") bastava para declarar lista e este HTML era
        rejeitado; agora a ausência de H2 numerados o devolve à política 2/4/6.
        """
        html = (
            "<p>Intro.</p>"
            "<h2>Tokyo Ghoul</h2>"
            "<p>Descrição do item.</p>"
        )
        report = validate_list_content("2 animes para assistir se você ama Frieren", html)
        self.assertFalse(report["is_list"])
        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
