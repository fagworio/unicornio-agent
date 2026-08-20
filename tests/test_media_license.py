import unittest

from unicornio_editor.media.license import LicenseError, validate_candidate


def candidate():
    return {
        "source_page_url": "https://commons.wikimedia.org/wiki/File:Game.jpg",
        "direct_image_url": "https://upload.wikimedia.org/game.jpg",
        "author": "Author Name",
        "license": "CC BY-SA 4.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "captured_at": "2026-08-20T12:00:00Z",
        "credit_text": "Author Name, CC BY-SA 4.0",
        "alt_text": "Imagem de um videogame",
    }


class LicenseTests(unittest.TestCase):
    def test_accepts_verifiable_creative_commons_candidate(self):
        result = validate_candidate(candidate())
        self.assertEqual(result["license"], "CC BY-SA 4.0")

    def test_rejects_missing_license(self):
        value = candidate()
        value["license"] = ""
        with self.assertRaises(LicenseError):
            validate_candidate(value)

    def test_rejects_google_preview_without_original_page(self):
        value = candidate()
        value["source_page_url"] = "https://images.google.com/imgres?imgurl=x"
        with self.assertRaises(LicenseError):
            validate_candidate(value)

    def test_rejects_unverified_all_rights_reserved(self):
        value = candidate()
        value["license"] = "All Rights Reserved"
        with self.assertRaises(LicenseError):
            validate_candidate(value)


if __name__ == "__main__":
    unittest.main()
