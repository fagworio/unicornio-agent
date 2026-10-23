import unittest

from unicornio_editor.media.license import (
    LicenseError,
    attest_source_rights,
    validate_candidate,
    validate_upload_candidate,
)
from unicornio_editor.workflow import _media_license_rejection


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

    def test_rejects_use_with_credit_without_a_license(self):
        value = candidate()
        value["source_page_url"] = "https://blog.example/noticia/one-piece-artigo"
        value["direct_image_url"] = "https://blog.example/media/one-piece-capa.jpg"
        value["license"] = "Uso com crédito"
        value["license_url"] = value["source_page_url"]
        value["credit_text"] = "Crédito da imagem: One Piece (arte de divulgação). Uso com crédito."
        with self.assertRaisesRegex(LicenseError, "credit does not grant permission"):
            validate_candidate(value)

    def test_accepts_explicit_compatible_license_and_reference(self):
        value = candidate()
        value["license"] = "CC BY 4.0"
        value["license_url"] = "https://creativecommons.org/licenses/by/4.0/"
        result = validate_candidate(value)
        self.assertEqual(result["license_url"], value["license_url"])

    def test_rejects_missing_license_reference_instead_of_inferring_source_page(self):
        value = candidate()
        value["license_url"] = ""
        with self.assertRaises(LicenseError):
            validate_candidate(value)

    def test_rejects_noncommercial_or_no_derivatives_licenses(self):
        for license_name in ("CC BY-NC 4.0", "CC BY-ND 4.0", "CC BY-NC-ND 4.0"):
            with self.subTest(license=license_name):
                value = candidate()
                value["license"] = license_name
                with self.assertRaises(LicenseError):
                    validate_candidate(value)

    def test_rejects_license_url_that_does_not_match_the_declared_license(self):
        value = candidate()
        value["license"] = "CC BY 4.0"
        value["license_url"] = "https://source.example/article"
        with self.assertRaisesRegex(LicenseError, "does not match"):
            validate_candidate(value)

    def test_rejects_mismatched_declared_and_reference_versions(self):
        value = candidate()
        value["license"] = "CC BY 4.0"
        value["license_url"] = "https://creativecommons.org/licenses/by/3.0/"
        with self.assertRaisesRegex(LicenseError, "does not match"):
            validate_candidate(value)

    def test_rejects_unrecognized_license_suffix_even_with_official_url(self):
        value = candidate()
        value["license"] = "CC BY 4.0 editorial use"
        value["license_url"] = "https://creativecommons.org/licenses/by/4.0/"
        with self.assertRaises(LicenseError):
            validate_candidate(value)

    def test_cc0_and_public_domain_references_are_version_bounded(self):
        value = candidate()
        value["license"] = "CC0"
        value["license_url"] = "https://creativecommons.org/publicdomain/zero/1.0/"
        validate_candidate(value)

        value["license"] = "Public Domain Mark 1.0"
        value["license_url"] = "https://creativecommons.org/publicdomain/mark/1.0/"
        validate_candidate(value)

        value["license"] = "CC0 1.0"
        value["license_url"] = "https://creativecommons.org/publicdomain/zero/2.0/"
        with self.assertRaisesRegex(LicenseError, "does not match"):
            validate_candidate(value)

        value["license"] = "Public Domain Mark 2.0"
        value["license_url"] = "https://creativecommons.org/publicdomain/mark/1.0/"
        with self.assertRaisesRegex(LicenseError, "does not match"):
            validate_candidate(value)

    def test_rejects_non_https_license_reference(self):
        value = candidate()
        value["license"] = "CC BY 4.0"
        value["license_url"] = "http://creativecommons.org/licenses/by/4.0/"
        with self.assertRaisesRegex(LicenseError, "does not match"):
            validate_candidate(value)

    def test_rejects_unstructured_permission_claim(self):
        value = candidate()
        value["license"] = "Permission granted"
        value["license_url"] = value["source_page_url"]
        with self.assertRaises(LicenseError):
            validate_candidate(value)

    def test_media_preflight_rejects_credit_only_before_apply(self):
        value = candidate()
        value["license"] = "Uso com crédito"
        reason = _media_license_rejection(value)
        self.assertIn("inválidos ou incompletos", reason)

    @staticmethod
    def _licensed_image_html(value):
        import json

        image_object = {
            "@context": "https://schema.org",
            "@type": "ImageObject",
            "contentUrl": value["direct_image_url"],
            "license": value["license_url"],
        }
        return '<script type="application/ld+json">' + json.dumps(image_object) + "</script>"

    def test_upload_requires_source_bound_rights_attestation(self):
        value = candidate()
        with self.assertRaisesRegex(LicenseError, "not proven"):
            validate_upload_candidate(value)

    def test_attests_license_only_for_exact_image_object(self):
        value = candidate()
        html = self._licensed_image_html(value)
        attestation = attest_source_rights(value, html)
        self.assertIsNotNone(attestation)
        validated = validate_upload_candidate({**value, "source_rights_attestation": attestation})
        self.assertEqual(validated["direct_image_url"], value["direct_image_url"])

    def test_rejects_page_level_license_without_image_binding(self):
        value = candidate()
        html = (
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage",'
            '"license":"https://creativecommons.org/licenses/by-sa/4.0/",'
            '"image":"https://upload.wikimedia.org/game.jpg"}'
            "</script>"
        )
        self.assertIsNone(attest_source_rights(value, html))

    def test_rejects_license_attached_to_a_different_image(self):
        value = candidate()
        html = self._licensed_image_html({
            **value,
            "direct_image_url": "https://upload.wikimedia.org/another-game.jpg",
        })
        self.assertIsNone(attest_source_rights(value, html))

    def test_rejects_source_license_mismatch(self):
        value = candidate()
        html = self._licensed_image_html({
            **value,
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
        })
        self.assertIsNone(attest_source_rights(value, html))

    def test_rejects_non_schema_image_object_type(self):
        value = candidate()
        html = (
            '<script type="application/ld+json">'
            '{"@type":"https://attacker.example/ImageObject",'
            '"contentUrl":"https://upload.wikimedia.org/game.jpg",'
            '"license":"https://creativecommons.org/licenses/by-sa/4.0/"}'
            "</script>"
        )
        self.assertIsNone(attest_source_rights(value, html))

    def test_invalid_image_url_in_structured_evidence_fails_closed(self):
        value = candidate()
        value["direct_image_url"] = "https://upload.wikimedia.org:invalid/game.jpg"
        self.assertIsNone(attest_source_rights(value, self._licensed_image_html(value)))

    def test_attestation_cannot_be_reused_for_another_image(self):
        value = candidate()
        proof = attest_source_rights(value, self._licensed_image_html(value))
        changed = {**value, "direct_image_url": "https://upload.wikimedia.org/other.jpg"}
        with self.assertRaisesRegex(LicenseError, "does not match"):
            validate_upload_candidate({**changed, "source_rights_attestation": proof})


if __name__ == "__main__":
    unittest.main()
