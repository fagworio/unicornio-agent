import unittest

from unicornio_editor.media.inserter import MediaInsertionError, append_featured_credit, insert_media


class MediaInserterTests(unittest.TestCase):
    def test_adds_one_visible_featured_credit(self):
        credit = "Crédito da imagem: Omelete. Imagem promocional do trailer. Direitos autorais dos detentores."
        result = append_featured_credit("<p>Texto.</p><p>Continuação.</p>", credit)
        self.assertIn('<p class="image-credit">' + credit + "</p>", result)
        self.assertEqual(append_featured_credit(result, credit), result)

    def test_inserts_figure_between_paragraphs_with_credit(self):
        html = "<p>Um.</p><p>Dois.</p><p>Três.</p><p>Quatro.</p>"
        result = insert_media(html, [{
            "paragraph_index": 1,
            "media_url": "http://wordpress.local/image.webp",
            "alt_text": "Imagem de jogo",
            "credit_text": "Crédito da imagem: Autor. Imagem de jogo. Domínio público (CC0).",
        }])
        self.assertIn('<figure class="aligncenter"><img src="http://wordpress.local/image.webp" alt="Imagem de jogo" />', result)
        self.assertIn("<figcaption>Crédito da imagem: Autor. Imagem de jogo. Domínio público (CC0).</figcaption>", result)
        self.assertLess(result.index('</p><figure class="aligncenter">'), result.index("<p>Três."))

    def test_rejects_insertion_inside_paragraph(self):
        with self.assertRaises(MediaInsertionError):
            insert_media("<p>Texto.</p>", [{
                "paragraph_index": 0,
                "media_url": "https://media.example/image.webp",
                "alt_text": "Imagem",
                "credit_text": "Crédito da imagem: Autor. Imagem de jogo. Domínio público (CC0).",
            }])

    def test_enforces_maximum_four_images(self):
        plan = [{
            "paragraph_index": i * 3 + 1,
            "media_url": f"https://media.example/{i}.webp",
            "alt_text": "Imagem",
            "credit_text": "Crédito da imagem: Autor. Imagem de jogo. Domínio público (CC0).",
        } for i in range(5)]
        with self.assertRaises(MediaInsertionError):
            insert_media("".join("<p>Texto.</p>" for _ in range(20)), plan)

    def test_enforces_distance_between_images(self):
        plan = [
            {"paragraph_index": 1, "media_url": "https://media.example/1.webp", "alt_text": "Um", "credit_text": "A / CC0"},
            {"paragraph_index": 3, "media_url": "https://media.example/2.webp", "alt_text": "Dois", "credit_text": "B / CC0"},
        ]
        with self.assertRaises(MediaInsertionError):
            insert_media("".join("<p>Texto.</p>" for _ in range(8)), plan)


if __name__ == "__main__":
    unittest.main()
