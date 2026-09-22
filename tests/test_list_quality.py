import unittest

from unicornio_editor.list_quality import ListContentError, detect_list_format, validate_list_content


GOOD = """
<p>Se você ama Frieren, estas obras podem agradar.</p>
<h2>2. Maquia: o peso de viver além das pessoas que você ama</h2>
<figure class="aligncenter"><img src="maquia.webp" /></figure><p>Maquia acompanha uma história sobre tempo e perda.</p>
<h2>1. Scrapped Princess: a ciência por trás da magia</h2>
<figure class="aligncenter"><img src="scrapped.webp" /></figure><p>A série constrói regras próprias para seu mundo.</p>
"""


class ListQualityTests(unittest.TestCase):
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
    def test_accepts_zero_image_listicle(self):
        html = (
            "<p>Intro.</p>"
            "<h2>1. Tokyo Ghoul: um horror que precisa respirar</h2>"
            "<p>Descrição do item.</p>"
            "<h2>2. Berserk: uma adaptação corajosa</h2>"
            "<p>Descrição do item 2.</p>"
        )
        report = validate_list_content("2 animes para assistir se você ama Frieren", html)
        self.assertTrue(report["passed"])

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
