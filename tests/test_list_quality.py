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
    def test_detects_promised_count(self):
        self.assertEqual(detect_list_format("10 animes para assistir se você ama Frieren"), 10)
        self.assertIsNone(detect_list_format("Bailarina ganha novo trailer"))

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


if __name__ == "__main__":
    unittest.main()
