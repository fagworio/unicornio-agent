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

    def test_media_plan_needs_source_and_alt_but_no_legal_metadata(self):
        payload = valid_payload()
        payload["media_plan"] = [{
            "paragraph_index": 0,
            "source_page_url": "https://publisher.example/story",
            "direct_image_url": "https://cdn.example/game.webp",
            "alt_text": "Cena do jogo",
            "is_featured": False,
        }]
        normalized = validate_editorial(payload)
        self.assertEqual(len(normalized["media_plan"]), 1)
        self.assertNotIn("license", normalized["media_plan"][0])

    def test_media_plan_accepts_item_scoped_list_targets_and_extra_placement(self):
        payload = valid_payload()
        payload["media_plan"] = [{
            "paragraph_index": 0,
            "placement": "after_list_item_description",
            "source_page_url": "https://publisher.example/story",
            "direct_image_url": "https://cdn.example/game.webp",
            "alt_text": "Hades II action",
            "credit_text": "Crédito da imagem: Publisher.",
            "is_featured": False,
            "target_id": "extra-1-root:number-1",
            "item_identity": "root:number-1",
            "item_title": "Hades II",
            "item_group": "Recomendações",
            "item_number": 1,
            "section_heading": "Hades II",
            "section_slot": 0,
            "html_position": 10,
        }]

        normalized = validate_editorial(payload)

        self.assertEqual(normalized["media_plan"][0]["item_identity"], "root:number-1")
        self.assertEqual(normalized["media_plan"][0]["placement"], "after_list_item_description")

    def test_media_plan_preserves_bounded_origin_evidence_for_apply_revalidation(self):
        payload = valid_payload()
        payload["media_plan"] = [{
            "paragraph_index": 0,
            "source_page_url": "https://publisher.example/story",
            "direct_image_url": "https://cdn.example/game.webp",
            "alt_text": "Cena do jogo",
            "is_featured": False,
            "origin_evidence": {
                "verdict": "deterministic_match",
                "score": 9,
                "local_score": 5,
                "subject": "Metroid Prime 4",
                "matched": ["page_title", "alt_original"],
                "fields": {
                    "page_title": "Metroid Prime 4 key art",
                    "alt_original": "Metroid Prime 4 artwork",
                    "page_url": "https://publisher.example/story",
                },
            },
        }]

        evidence = validate_editorial(payload)["media_plan"][0]["origin_evidence"]

        self.assertEqual(evidence["fields"]["alt_original"], "Metroid Prime 4 artwork")
        self.assertEqual(evidence["score"], 9.0)

    def test_media_plan_rejects_unbounded_or_unknown_origin_evidence(self):
        payload = valid_payload()
        payload["media_plan"] = [{
            "paragraph_index": 0,
            "source_page_url": "https://publisher.example/story",
            "direct_image_url": "https://cdn.example/game.webp",
            "alt_text": "Cena do jogo",
            "is_featured": False,
            "origin_evidence": {
                "fields": {"page_title": "x" * 501},
                "secret": "must not cross the schema boundary",
            },
        }]

        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload)

    def test_historical_legal_fields_are_readable_but_removed_from_active_plan(self):
        payload = valid_payload()
        payload["media_plan"] = [{
            "paragraph_index": 0,
            "source_page_url": "https://publisher.example/story",
            "direct_image_url": "https://cdn.example/game.webp",
            "alt_text": "Cena do jogo",
            "is_featured": False,
            "license": "legacy license",
            "license_url": "https://rights.example/terms",
            "rights_verified": False,
            "rights_decision": {"status": "UNPROVEN"},
            "author": "Legacy author",
            "credit_text": "Legacy credit",
        }]
        normalized = validate_editorial(payload)
        self.assertEqual(set(normalized["media_plan"][0]), {
            "paragraph_index", "source_page_url", "direct_image_url", "alt_text", "is_featured",
            "credit_text",
        })
        self.assertEqual(normalized["media_plan"][0]["credit_text"], "Legacy credit")

    def test_featured_media_target_id_does_not_require_list_item_identity(self):
        payload = valid_payload()
        payload["media_plan"] = [{
            "paragraph_index": 0,
            "source_page_url": "https://publisher.example/story",
            "direct_image_url": "https://cdn.example/game.webp",
            "alt_text": "Arte principal do jogo",
            "is_featured": True,
            "target_id": "featured",
        }]

        normalized = validate_editorial(payload)

        self.assertEqual(normalized["media_plan"][0]["target_id"], "featured")

    def test_media_exhausted_optional_boolean(self):
        # media_exhausted e opcional (default ausente) e deve ser booleano.
        result = validate_editorial(valid_payload())
        self.assertNotIn("media_exhausted", result)
        payload = valid_payload()
        payload["media_exhausted"] = True
        self.assertTrue(validate_editorial(payload).get("media_exhausted"))

    def test_media_exhausted_rejects_non_boolean(self):
        payload = valid_payload()
        payload["media_exhausted"] = "sim"
        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload)

    def test_accepts_audited_trailer_unavailable_waiver(self):
        payload = valid_payload()
        payload["game_name"] = "Jogo sem trailer"
        payload["trailer_unavailable"] = True
        payload["trailer_search_evidence"] = {
            "query": "Jogo sem trailer trailer",
            "provider": "youtube",
            "searched_at": "2026-09-03T12:00:00+00:00",
            "result": "official_not_found",
        }
        self.assertTrue(validate_editorial(payload)["trailer_unavailable"])

    def test_rejects_unaudited_trailer_waiver(self):
        payload = valid_payload()
        payload["game_name"] = "Jogo sem trailer"
        payload["trailer_unavailable"] = True
        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload)

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

    def test_accepts_missing_seo(self):
        payload = valid_payload()
        del payload["seo"]
        self.assertIsNone(validate_editorial(payload).get("seo"))

    def test_accepts_null_seo(self):
        payload = valid_payload()
        payload["seo"] = None
        self.assertIsNone(validate_editorial(payload)["seo"])

    def test_rejects_invalid_seo_when_present(self):
        payload = valid_payload()
        payload["seo"] = {"title": "x" * 66, "meta_description": "curta", "focus_keyword": "jogo"}
        with self.assertRaises(EditorialValidationError):
            validate_editorial(payload)

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

    def test_accepts_media_without_legal_metadata(self):
        payload = valid_payload()
        payload["media_plan"] = [{
            "paragraph_index": 1,
            "source_page_url": "https://source.example/page",
            "direct_image_url": "https://source.example/image.jpg",
            "alt_text": "Imagem ilustrativa",
            "is_featured": False,
        }]
        normalized = validate_editorial(payload)
        self.assertEqual(
            normalized["media_plan"][0]["source_page_url"],
            "https://source.example/page",
        )

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
