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

    def test_fonte_extrai_multiplos_assets_sem_resolver_nova_origem(self):
        html = '''
        <meta property="og:image" content="https://cdn.example/hero.jpg">
        <article>
          <img src="https://cdn.example/frame-a.webp" width="1200" height="675" alt="Jogo">
          <img src="https://cdn.example/logo.png" width="1200" height="800">
          <img src="https://cdn.example/frame-b.webp" width="1280" height="720" alt="Jogo">
        </article>
        '''
        stats = {}
        with mock.patch.object(source_verify, "_fetch", return_value=html.encode("utf-8")):
            candidates = source_verify.discover_article_source_candidates(
                "https://source.example/news", subject="Jogo", stats=stats
            )
        urls = {item["direct_image_url"] for item in candidates}
        self.assertIn("https://cdn.example/hero.jpg", urls)
        self.assertIn("https://cdn.example/frame-a.webp", urls)
        self.assertIn("https://cdn.example/frame-b.webp", urls)
        self.assertNotIn("https://cdn.example/logo.png", urls)
        self.assertEqual(stats, {"raw_assets": 4, "editorial_assets": 4, "filtered_assets": 0})
        self.assertTrue(all(item["source_origin_type"] == "article_source" for item in candidates))
        self.assertTrue(all(item["evidence"]["verdict"] == "deterministic_match" for item in candidates))

    def test_fonte_ignora_related_e_preserva_subjects_editoriais(self):
        html = """
        <article><main>
          <img src="https://cdn.example/rpcs3.jpg" width="1200" height="675" alt="RPCS3">
          <div class="related-news"><img src="https://cdn.example/gta-6.jpg" width="1200" height="675"></div>
        </main></article>
        """
        with mock.patch.object(source_verify, "_fetch", return_value=html.encode("utf-8")):
            candidates = source_verify.discover_article_source_candidates(
                "https://source.example/news",
                subject="RPCS3",
                subjects=["RPCS3", "PlayStation 5", "emulação"],
            )
        assert [item["direct_image_url"] for item in candidates] == [
            "https://cdn.example/rpcs3.jpg"
        ]
        assert candidates[0]["subjects"] == ["RPCS3", "PlayStation 5", "emulação"]
        assert candidates[0]["source_context_kind"] == "article_body"

    def test_fonte_descarta_asset_sem_contexto_editorial(self):
        html = '<img src="https://cdn.example/card.jpg" width="1200" height="675">'
        with mock.patch.object(source_verify, "_fetch", return_value=html.encode("utf-8")):
            candidates = source_verify.discover_article_source_candidates(
                "https://source.example/news", subject="Jogo"
            )
        assert candidates == []


if __name__ == "__main__":
    unittest.main()
