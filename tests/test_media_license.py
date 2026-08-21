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
        "credit_text": "Crédito da imagem: Author Name. Imagem do jogo. Licença CC BY-SA 4.0.",
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

    def test_rejects_all_rights_reserved_without_credit_marker(self):
        value = candidate()
        value["license"] = "All Rights Reserved"
        with self.assertRaises(LicenseError):
            validate_candidate(value)

    def test_accepts_google_image_with_use_with_credit_marker(self):
        # Policy 2026-08: any web image is usable with a visible credit —
        # the credit block is the evidence, no free license required.
        value = candidate()
        value["source_page_url"] = "https://blog.example/noticia/one-piece-artigo"
        value["direct_image_url"] = "https://blog.example/media/one-piece-capa.jpg"
        value["license"] = "Uso com crédito"
        value["license_url"] = ""
        value["credit_text"] = "Crédito da imagem: One Piece (arte de divulgação). Uso com crédito."
        result = validate_candidate(value)
        self.assertEqual(result["license"], "Uso com crédito")
        # Sem pagina de licenca, a pagina original da imagem vira a referencia.
        self.assertEqual(result["license_url"], value["source_page_url"])

    def test_accepts_use_with_credit_with_explicit_license_url(self):
        value = candidate()
        value["license"] = "Uso com crédito"
        value["license_url"] = "https://blog.example/sobre/uso-de-imagens"
        value["credit_text"] = "Crédito da imagem: One Piece. Uso com crédito (https://blog.example/sobre/uso-de-imagens)."
        result = validate_candidate(value)
        self.assertEqual(result["license_url"], "https://blog.example/sobre/uso-de-imagens")


if __name__ == "__main__":
    unittest.main()
