import unittest

from unicornio_editor.editorial_schema import EditorialValidationError, validate_editorial


def valid_payload():
    return {
        "site_relevance": {
            "decision": "process",
            "confidence": 0.96,
            "reason": "Notícia sobre videogame",
            "matched_topics": ["games"],
        },
        "cleaned_html": "<p>Texto revisado.</p>",
        "seo": {
            "title": "Notícia sobre videogame e lançamento importante",
            "meta_description": "Uma notícia completa sobre videogame, lançamento, plataformas e os principais detalhes para o leitor entender o assunto.",
            "focus_keyword": "notícia sobre videogame",
        },
        "media_plan": [],
        "needs_trailer": False,
        "trailer_url": None,
        "game_name": None,
    }


class EditorialSchemaTests(unittest.TestCase):
    def test_accepts_valid_payload(self):
        self.assertEqual(validate_editorial(valid_payload())["cleaned_html"], "<p>Texto revisado.</p>")

    def test_rejects_unknown_top_level_field(self):
        payload = valid_payload()
        payload["publish"] = True
        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload)

    def test_rejects_low_confidence_process_decision(self):
        payload = valid_payload()
        payload["site_relevance"]["confidence"] = 0.2
        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload, min_confidence=0.8)

    def test_accepts_skip_without_editorial_changes(self):
        payload = valid_payload()
        payload["site_relevance"] = {
            "decision": "skip",
            "confidence": 0.99,
            "reason": "Conteúdo fora da linha editorial",
            "matched_topics": [],
        }
        payload["cleaned_html"] = ""
        self.assertEqual(validate_editorial(payload)["site_relevance"]["decision"], "skip")

    def test_accepts_missing_cleaned_html(self):
        payload = valid_payload()
        del payload["cleaned_html"]
        self.assertIsNone(validate_editorial(payload).get("cleaned_html"))

    def test_accepts_null_cleaned_html(self):
        payload = valid_payload()
        payload["cleaned_html"] = None
        self.assertIsNone(validate_editorial(payload)["cleaned_html"])

    def test_rejects_non_string_cleaned_html(self):
        payload = valid_payload()
        payload["cleaned_html"] = 123
        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload)

    def test_rejects_seo_title_over_65_chars(self):
        payload = valid_payload()
        payload["seo"]["title"] = "x" * 66
        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload)

    def test_rejects_media_without_license_evidence(self):
        payload = valid_payload()
        payload["media_plan"] = [{
            "paragraph_index": 1,
            "source_page_url": "https://source.example/page",
            "direct_image_url": "https://source.example/image.jpg",
            "author": "Autor",
            "license": "",
            "license_url": "https://source.example/license",
            "captured_at": "2026-08-20T12:00:00Z",
            "credit_text": "Imagem: Autor / Source",
            "alt_text": "Imagem ilustrativa",
            "is_featured": False,
        }]
        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload)

    def test_rejects_more_than_one_featured_image(self):
        payload = valid_payload()
        base = {
            "paragraph_index": 0,
            "source_page_url": "https://source.example/page",
            "direct_image_url": "https://source.example/image.jpg",
            "author": "Autor",
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "captured_at": "2026-08-20T12:00:00Z",
            "credit_text": "Crédito da imagem: Autor. Imagem. CC BY 4.0.",
            "alt_text": "Imagem",
        }
        payload["media_plan"] = [
            {**base, "paragraph_index": 0, "is_featured": True},
            {**base, "paragraph_index": 3, "is_featured": True},
        ]
        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload)

    def test_accepts_game_name_string(self):
        payload = valid_payload()
        payload["game_name"] = "Clive Barker's Hellraiser: Revival"
        self.assertEqual(
            validate_editorial(payload)["game_name"],
            "Clive Barker's Hellraiser: Revival",
        )

    def test_rejects_game_name_non_string(self):
        payload = valid_payload()
        payload["game_name"] = 123
        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload)

    def test_rejects_blank_game_name(self):
        payload = valid_payload()
        payload["game_name"] = "   "
        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload)


if __name__ == "__main__":
    unittest.main()
