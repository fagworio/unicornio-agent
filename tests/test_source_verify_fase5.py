"""Fase 5 — validação de origem do candidato ANTES do media_plan."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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

    def test_source_page_html_is_cached_for_rights_validation(self):
        page = "https://page.example/source"
        cache = {}
        with mock.patch.object(
            source_verify, "_fetch", return_value=b"<html>source</html>"
        ) as fetch:
            first = source_verify.source_page_html(page, cache=cache)
            second = source_verify.source_page_html(page, cache=cache)
        self.assertEqual(first, "<html>source</html>")
        self.assertEqual(second, first)
        fetch.assert_called_once()

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
        self.assertIn(out["reason_code"], {
            "source_mismatch", "source_scan_incomplete", "source_decode_failed"
        })

    def test_pagina_inacessivel_e_invalida(self):
        with mock.patch.object(source_verify, "_fetch", return_value=None):
            out = source_verify.validate_discovered_candidate(
                {"direct_image_url": "https://cdn/x.jpg",
                 "source_page_url": "https://page.example/x/"}
            )
        self.assertFalse(out["valid"])
        self.assertIn("inacessivel", out["reason"])

    def test_candidato_indisponivel_nao_vira_source_mismatch(self):
        html = '<article><img src="https://source.example/other.jpg"></article>'
        with mock.patch.object(
            source_verify, "_fetch", side_effect=[html.encode("utf-8"), None]
        ):
            out = source_verify.validate_discovered_candidate({
                "direct_image_url": "https://cdn.example/opaque-copy.jpg",
                "source_page_url": "https://page.example/story",
            })
        self.assertEqual(out["reason_code"], "candidate_asset_unavailable")
        self.assertTrue(out["recoverable"])
        self.assertFalse(out["source_comparison_proved"])

    def test_varredura_acima_do_limite_e_incompleta_nao_mismatch(self):
        html = "<article>" + "".join(
            f'<img src="https://source.example/asset-{i}.jpg">' for i in range(13)
        ) + "</article>"
        image_bytes = b"image-bytes"
        calls = 0

        def fetch(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return html.encode() if calls == 1 else image_bytes

        with mock.patch.object(source_verify, "_fetch", side_effect=fetch), mock.patch.object(
            source_verify, "verify_image_identity", return_value=(False, "MISMATCH")
        ):
            out = source_verify.validate_discovered_candidate({
                "direct_image_url": "https://discovery.example/candidate.jpg",
                "source_page_url": "https://page.example/story",
            })
        self.assertEqual(out["reason_code"], "source_scan_incomplete")
        self.assertEqual(out["comparisons_attempted"], 12)
        self.assertEqual(out["assets_considered"], 13)
        self.assertTrue(out["recoverable"])
        self.assertFalse(out["source_comparison_proved"])

    def test_matching_asset_at_position_eleven_is_recovered(self):
        html = "<article>" + "".join(
            f'<img src="https://source.example/asset-{i}.jpg">' for i in range(11)
        ) + "</article>"
        comparison_results = [(False, "MISMATCH")] * 10 + [(True, "PIXEL_IDENTICAL")]

        def fetch(url, *_args, **_kwargs):
            return html.encode() if url == "https://page.example/story" else b"image-bytes"

        with mock.patch.object(source_verify, "_fetch", side_effect=fetch), mock.patch.object(
            source_verify, "verify_image_identity", side_effect=comparison_results
        ):
            out = source_verify.validate_discovered_candidate({
                "direct_image_url": "https://discovery.example/candidate.jpg",
                "source_page_url": "https://page.example/story",
            })

        self.assertTrue(out["valid"])
        self.assertEqual(out["verification_status"], "verified")
        self.assertEqual(out["comparisons_attempted"], 11)
        self.assertEqual(out["matched_image_url"], "https://source.example/asset-10.jpg")

    def test_eleventh_preflight_asset_is_finally_verified_by_its_canonical_url(self):
        page_url = "https://page.example/story"
        html = "<article>" + "".join(
            f'<img src="https://source.example/asset-{i}.jpg">' for i in range(11)
        ) + "</article>"
        match_results = [(False, "MISMATCH")] * 10 + [(True, "PIXEL_IDENTICAL")]

        def preflight_fetch(url, *_args, **_kwargs):
            return html.encode() if url == page_url else b"same-image-bytes"

        with mock.patch.object(source_verify, "_fetch", side_effect=preflight_fetch), mock.patch.object(
            source_verify, "verify_image_identity", side_effect=match_results
        ):
            preflight = source_verify.validate_discovered_candidate({
                "direct_image_url": "https://discovery.example/opaque.jpg",
                "source_page_url": page_url,
            })
        self.assertTrue(preflight["valid"])
        canonical = preflight["matched_image_url"]
        self.assertEqual(canonical, "https://source.example/asset-10.jpg")

        with TemporaryDirectory() as directory:
            downloaded = Path(directory) / "asset.img"
            downloaded.write_bytes(b"same-image-bytes")
            final_calls = []

            def final_fetch(url, *_args, **_kwargs):
                final_calls.append(url)
                return html.encode() if url == page_url else b"same-image-bytes"

            with mock.patch.object(source_verify, "_fetch", side_effect=final_fetch):
                ok, reason = source_verify.verify_downloaded_against_source(
                    source_page_url=page_url,
                    downloaded=downloaded,
                    direct_image_url=canonical,
                )

        self.assertTrue(ok, reason)
        self.assertEqual(final_calls, [page_url, canonical])

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
        self.assertTrue(all(item["evidence"]["verdict"] != "deterministic_match" or item["evidence_score"] < 100 for item in candidates))

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

    def test_artigo_comum_nao_descarta_imagem_ao_adicionar_entidade_secundaria(self):
        html = """<html><head><title>Metroid Prime 4 revela detalhes</title></head>
        <article><h2>Metroid Prime 4</h2><figure>
        <img src="https://cdn.example/opaque.jpg" alt="Metroid Prime 4 key art">
        <figcaption>Arte de Metroid Prime 4</figcaption></figure></article></html>"""
        with mock.patch.object(source_verify, "_fetch", return_value=html.encode()):
            candidates = source_verify.discover_article_source_candidates(
                "https://source.example/story", subjects=["Metroid Prime 4", "Halo Infinite"],
                is_listicle=False,
            )
        assert len(candidates) == 1
        assert candidates[0]["subject"] == "Metroid Prime 4"
        assert candidates[0]["evidence"]["score"] < 100

    def test_fonte_descarta_asset_sem_contexto_editorial(self):
        html = '<img src="https://cdn.example/card.jpg" width="1200" height="675">'
        with mock.patch.object(source_verify, "_fetch", return_value=html.encode("utf-8")):
            candidates = source_verify.discover_article_source_candidates(
                "https://source.example/news", subject="Jogo"
            )
        assert candidates == []


if __name__ == "__main__":
    unittest.main()
