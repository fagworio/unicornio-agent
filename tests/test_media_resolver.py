"""SourceResolver + fluxo ponta a ponta da mídia determinística."""

import unittest

from unicornio_editor.media.evidence import evidence_score, source_context
from unicornio_editor.media.source_resolver import (
    _queries,
    _slug_do_filename,
    resolve_candidate_source,
)


class ResolverTests(unittest.TestCase):
    def _cand(self):
        return {
            "direct_image_url": "https://cdn.aggregator.com/metroid-prime-4-beyond-keyart.jpg",
            "source_page_url": "",
            "usable": False,
            "discovery_only": True,
        }

    def test_slug_do_filename_vira_termo_de_busca(self):
        self.assertEqual(
            _slug_do_filename("https://cdn.x/metroid-prime-4-beyond-keyart.jpg"),
            "metroid prime 4 beyond keyart",
        )

    def test_estrategia_c_prioriza_dominio_oficial(self):
        queries = _queries(self._cand(), "metroid prime 4")
        self.assertTrue(queries[0].startswith("site:nintendo.com"))
        self.assertTrue(any(q.startswith('"metroid') for q in queries))

    def test_resolve_pela_estrategia_c(self):
        def busca(query):
            if query.startswith("site:nintendo.com"):
                return [{"source_page_url": "https://www.nintendo.com/games/metroid-prime-4/"}]
            return []

        out = resolve_candidate_source(self._cand(), "metroid prime 4", busca=busca)
        self.assertEqual(out["source_page_url"], "https://www.nintendo.com/games/metroid-prime-4/")
        self.assertEqual(out["source_resolution"], "official_domain")

    def test_sem_achar_permanece_unresolved_sem_origem(self):
        out = resolve_candidate_source(self._cand(), "metroid prime 4", busca=lambda q: [])
        self.assertEqual(out["source_resolution"], "unresolved")
        self.assertFalse(out["source_page_url"])
        # e o gate de proveniência continua barrando
        pontos = evidence_score(
            "metroid prime 4",
            filename="metroid-prime-4.jpg",
            source_page_present=bool(out["source_page_url"]),
        )
        self.assertEqual(pontos["verdict"], "unresolved_source")

    def test_aceita_pagina_no_mesmo_host_da_imagem(self):
        """Acceptance 6: página e imagem no MESMO domínio é origem válida.

        example.com/materia + example.com/imagem.jpg é uma origem excelente —
        antes o resolver descartava por compartilhar host. O que continua
        inválido é a página SER a própria imagem (URL crua de arquivo).
        """
        out = resolve_candidate_source(
            self._cand(),
            "metroid prime 4",
            busca=lambda q: [{"source_page_url": "https://cdn.aggregator.com/materia/metroid"}],
        )
        self.assertEqual(out["source_page_url"], "https://cdn.aggregator.com/materia/metroid")

    def test_rejeita_pagina_que_e_a_propria_imagem(self):
        out = resolve_candidate_source(
            self._cand(),
            "metroid prime 4",
            busca=lambda q: [
                {"source_page_url": "https://cdn.aggregator.com/metroid.jpg"},
                {"source_page_url": "https://outro.com/materia"},
            ],
        )
        # a URL crua da imagem é descartada; a matéria permanece
        self.assertEqual(out["source_page_url"], "https://outro.com/materia")

    def test_verifier_escolhe_a_pagina_que_realmente_contem_a_imagem(self):
        """Acceptance 5: a 1ª página não tem a imagem, a 2ª tem -> usa a 2ª."""
        chamadas: list[str] = []

        def verifier(cand):
            chamadas.append(cand["source_page_url"])
            return {"valid": cand["source_page_url"].endswith("/jogo/")}

        def busca(q):
            return [
                {"source_page_url": "https://nintendo.com/news/metroid"},
                {"source_page_url": "https://nintendo.com/games/metroid/jogo/"},
            ]

        out = resolve_candidate_source(
            self._cand(), "metroid prime 4", busca=busca, verifier=verifier
        )
        self.assertEqual(out["source_page_url"], "https://nintendo.com/games/metroid/jogo/")
        self.assertEqual(out["source_resolution"], "verified_page")
        self.assertEqual(len(chamadas), 2)  # tentou as duas, na ordem


class FluxoPontaAPontaTests(unittest.TestCase):
    """Cadeia completa: descoberta -> origem -> contexto -> score -> veredito."""

    def _pagina(self):
        return (
            "<html><head><title>Metroid Prime 4: Beyond - Nintendo Official</title>"
            '<meta property="og:title" content="Metroid Prime 4: Beyond">'
            "</head><body><h1>Metroid Prime 4</h1>"
            '<figure><img src="https://assets.nintendo.com/metroid-prime-4-keyart.jpg" '
            'alt="Metroid Prime 4 key art"><figcaption>Arte oficial</figcaption></figure>'
            "</body></html>"
        )

    def test_key_art_oficial_atravessa_todos_os_gates(self):
        url = "https://assets.nintendo.com/metroid-prime-4-keyart.jpg"
        ctx = source_context(self._pagina(), url, base_url="https://www.nintendo.com/games/metroid-prime-4/")
        out = evidence_score(
            "metroid prime 4",
            filename="metroid-prime-4-keyart.jpg",
            og_title=ctx.get("og_title", ""),
            page_title=ctx.get("page_title", ""),
            alt_original=ctx.get("alt_original", ""),
            figcaption=ctx.get("figcaption", ""),
            heading=ctx.get("heading", ""),
            page_url=ctx.get("page_url", ""),
            query="metroid prime 4",
        )
        self.assertEqual(out["verdict"], "deterministic_match")
        self.assertFalse(out["needs_vision"])

    def test_imagem_de_outro_assunto_nao_passa_mesmo_com_origem(self):
        """O caso real do Bing degradado: página existe, assunto não é o do post."""
        html = '<html><head><title>Penguin Face Paint Ideas</title></head><body><img src="https://cdn.paint.com/penguin.jpg" alt="penguin face paint"></body></html>'
        url = "https://cdn.paint.com/penguin.jpg"
        ctx = source_context(html, url, base_url="https://paint.com/penguin/")
        out = evidence_score(
            "metroid prime 4",
            filename="penguin.jpg",
            og_title=ctx.get("og_title", ""),
            page_title=ctx.get("page_title", ""),
            alt_original=ctx.get("alt_original", ""),
            page_url=ctx.get("page_url", ""),
            query="metroid prime 4",
        )
        self.assertEqual(out["verdict"], "reject")

    def test_listicle_nao_empresta_evidencia_entre_itens(self):
        from unicornio_editor.media.evidence import post_subjects, subject_for_image

        subs = post_subjects(
            title="animes",
            content_html="<h2>1. Bleach</h2><h2>2. Pluto</h2>",
        )
        self.assertEqual(subject_for_image("https://x/pluto-keyart.jpg", subs), "Pluto")
        self.assertEqual(subject_for_image("https://x/bleach-keyart.jpg", subs), "Bleach")
        # imagem sem relação com item nenhum não herda subject global
        self.assertEqual(subject_for_image("https://x/naruto.jpg", subs), "")


if __name__ == "__main__":
    unittest.main()
