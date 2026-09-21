"""Fase 5 — validação de origem do candidato ANTES do media_plan."""

import unittest
from unittest import mock

from unicornio_editor.media import source_verify


class Fase5ValidateCandidateTests(unittest.TestCase):
    def _rodar(self, candidate, html):
        with mock.patch.object(
            source_verify, "_fetch", return_value=html.encode("utf-8")
        ):
            return source_verify.validate_discovered_candidate(candidate)

    def test_candidato_sem_source_page_e_invalido(self):
        out = source_verify.validate_discovered_candidate(
            {"direct_image_url": "https://cdn/x.jpg", "source_page_url": ""}
        )
        self.assertFalse(out["valid"])
        self.assertIn("source_page_url", out["reason"])

    def test_imagem_listada_na_pagina_e_valida(self):
        html = '<html><body><img src="https://cdn.example/bleach-keyart.jpg"></body></html>'
        out = self._rodar(
            {"direct_image_url": "https://cdn.example/bleach-keyart.jpg",
             "source_page_url": "https://page.example/bleach/"}, html)
        self.assertTrue(out["valid"])
        self.assertTrue(out["source_verified"])
        self.assertEqual(out["images_in_page"], 1)

    def test_imagem_ausente_na_pagina_e_invalida(self):
        html = '<html><body><img src="https://cdn.example/outra.jpg"></body></html>'
        out = self._rodar(
            {"direct_image_url": "https://cdn.example/naruto.jpg",
             "source_page_url": "https://page.example/bleach/"}, html)
        self.assertFalse(out["valid"])
        self.assertIn("nao listada", out["reason"])

    def test_pagina_inacessivel_e_invalida(self):
        with mock.patch.object(source_verify, "_fetch", return_value=None):
            out = source_verify.validate_discovered_candidate(
                {"direct_image_url": "https://cdn/x.jpg",
                 "source_page_url": "https://page.example/x/"}
            )
        self.assertFalse(out["valid"])
        self.assertIn("inacessivel", out["reason"])

    def test_url_sem_esquema_e_invalida(self):
        out = source_verify.validate_discovered_candidate(
            {"direct_image_url": "ftp://x/y.jpg", "source_page_url": "https://p/x"}
        )
        self.assertFalse(out["valid"])


if __name__ == "__main__":
    unittest.main()
