"""Fases 7-11 — subject, contexto de origem e score determinístico."""

import unittest

from unicornio_editor.media.evidence import (
    LIMIAR_AMBIGUO,
    LIMIAR_MATCH,
    evidence_score,
    post_subjects,
    source_context,
    subject_for_image,
)


class SubjectTests(unittest.TestCase):
    def test_artigo_normal_extrai_a_entidade_principal(self):
        """Nunca o título literal: "Metroid Prime 4", não a manchete inteira."""
        subs = post_subjects(
            title="Nintendo anuncia novo trailer de Metroid Prime 4", content_html="<p>x</p>"
        )
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["subject"], "metroid prime 4")

    def test_listicle_um_subject_por_h2_com_o_nome_completo(self):
        subs = post_subjects(
            title="10 melhores animes",
            content_html=(
                "<h2>1. Cyberpunk: Edgerunners</h2><p>a</p>"
                "<h2>2. Pluto</h2><p>b</p>"
            ),
        )
        self.assertEqual([s["item"] for s in subs], [1, 2])
        self.assertEqual(subs[0]["subject"], "Cyberpunk: Edgerunners")
        self.assertEqual(subs[1]["subject"], "Pluto")

    def test_subject_por_imagem_no_listicle_nao_vaza_entre_itens(self):
        subs = post_subjects(
            title="animes",
            content_html="<h2>1. Bleach</h2><h2>2. Pluto</h2>",
        )
        self.assertEqual(subject_for_image("https://x/pluto-keyart.jpg", subs), "Pluto")
        # imagem sem evidencia de nenhum item -> sem subject (nao herda o global)
        self.assertEqual(subject_for_image("https://x/random.jpg", subs), "")


class ScoreTests(unittest.TestCase):
    def test_key_art_com_evidencia_de_origem_e_match(self):
        out = evidence_score(
            "metroid prime 4",
            filename="Metroid-Prime-4-boxart.jpg",
            og_title="Metroid Prime 4: Beyond - Nintendo",
            page_title="Metroid Prime 4 Beyond boxart",
            alt_original="Metroid Prime 4 key art",
            page_url="https://nintendoeverything.com/metroid-prime-4-beyond-boxart/",
            query="Metroid Prime 4 Beyond",
        )
        self.assertEqual(out["verdict"], "deterministic_match")
        self.assertGreaterEqual(out["score"], LIMIAR_MATCH)
        self.assertFalse(out["needs_vision"])

    def test_imagem_irrelevante_com_origem_confirmada_e_rejeitada(self):
        """O caso real: origem OK, conteúdo errado (pinguim p/ Metroid)."""
        out = evidence_score(
            "metroid prime 4",
            filename="Penguin_Face_Paint_Design_grande.png",
            page_title="Penguin Face Design by Ana Cedoviste - Facepaint.com",
            page_url="https://www.facepaint.com/blogs/facepaint-blog/penguin-face-design",
            query="Metroid Prime 4 Beyond",
        )
        self.assertEqual(out["verdict"], "reject")
        self.assertLess(out["score"], LIMIAR_AMBIGUO)

    def test_ambiguo_quando_so_a_url_da_pagina_cita(self):
        out = evidence_score(
            "metroid prime 4",
            filename="hero.jpg",
            page_url="https://site.com/metroid-prime-4-review/",
        )
        self.assertEqual(out["verdict"], "reject")  # 2 pontos
        out2 = evidence_score(
            "metroid prime 4",
            filename="metroid-prime-4.jpg",
            page_url="https://site.com/materia/",
        )
        self.assertEqual(out2["verdict"], "ambiguous")
        self.assertTrue(out2["needs_vision"])

    def test_penalidades_objetivas(self):
        sem_source = evidence_score("metroid", filename="metroid.jpg", source_page_present=False)
        self.assertIn("missing_source", sem_source["penalties"])
        nao_listada = evidence_score("metroid", filename="metroid.jpg", image_in_source=False)
        self.assertIn("not_in_source", nao_listada["penalties"])
        dup = evidence_score("metroid", filename="metroid.jpg", duplicate_frame=True)
        self.assertIn("duplicate_frame", dup["penalties"])

    def test_alt_do_agente_nao_e_evidencia(self):
        """Fase 6: alt/caption escritos pelo agente não contam como prova.

        O score só recebe evidência de origem — não existe parâmetro para o
        texto gerado pelo agente.
        """
        params = evidence_score.__doc__ or ""
        self.assertNotIn("agent_alt", evidence_score.__code__.co_varnames)
        self.assertIn("alt_original", evidence_score.__code__.co_varnames)
        self.assertTrue(params)


class SourceContextTests(unittest.TestCase):
    def test_extrai_contexto_da_pagina_de_origem(self):
        html = (
            "<html><head><title>Bleach TYBW key art</title>"
            '<meta property="og:title" content="Bleach Thousand-Year Blood War">'
            "</head><body><h1>Bleach</h1>"
            '<figure><img src="https://cdn/bleach-keyart.jpg" alt="Bleach key art">'
            "<figcaption>Arte oficial de Bleach</figcaption></figure></body></html>"
        )
        ctx = source_context(html, "https://cdn/bleach-keyart.jpg", base_url="https://page/bleach/")
        self.assertEqual(ctx["page_title"], "Bleach TYBW key art")
        self.assertEqual(ctx["og_title"], "Bleach Thousand-Year Blood War")
        self.assertEqual(ctx["alt_original"], "Bleach key art")
        self.assertEqual(ctx["figcaption"], "Arte oficial de Bleach")
        self.assertEqual(ctx["heading"], "Bleach")
        self.assertEqual(ctx["filename"], "bleach-keyart.jpg")

    def test_contexto_vazio_para_html_vazio(self):
        self.assertEqual(source_context("", "https://x/a.jpg"), {})


class GateTests(unittest.TestCase):
    """Proveniência é GATE (hard), não penalidade: nenhuma soma de relevância
    pode compensar ausência de origem."""

    def test_sem_origem_e_unresolved_source_mesmo_com_score_maximo(self):
        out = evidence_score(
            "metroid prime 4",
            filename="metroid-prime-4-keyart.jpg",
            og_title="Metroid Prime 4",
            page_title="Metroid Prime 4",
            alt_original="Metroid Prime 4 key art",
            figcaption="Metroid Prime 4",
            heading="Metroid Prime 4",
            page_url="https://x/metroid-prime-4/",
            query="Metroid Prime 4",
            source_page_present=False,
        )
        self.assertEqual(out["verdict"], "unresolved_source")
        self.assertEqual(out["gate"], "provenance")
        self.assertEqual(out["score"], 0)
        self.assertFalse(out["needs_vision"])

    def test_imagem_ausente_da_pagina_e_source_mismatch(self):
        out = evidence_score("metroid", filename="metroid.jpg", image_in_source=False)
        self.assertEqual(out["verdict"], "source_mismatch")
        self.assertEqual(out["gate"], "provenance")

    def test_frame_duplicado_e_gate_de_diversidade(self):
        out = evidence_score(
            "metroid", filename="metroid.jpg", og_title="Metroid", duplicate_frame=True
        )
        self.assertEqual(out["verdict"], "duplicate_frame")
        self.assertEqual(out["gate"], "diversity")

    def test_visao_so_em_ambiguo_com_origem_valida(self):
        ambiguo = evidence_score("metroid prime 4", filename="metroid-prime-4.jpg")
        self.assertEqual(ambiguo["verdict"], "ambiguous")
        self.assertTrue(ambiguo["needs_vision"])
        assertivo = evidence_score(
            "metroid prime 4",
            filename="metroid-prime-4.jpg",
            og_title="Metroid Prime 4: Beyond",
            page_title="Metroid Prime 4 review",
        )
        self.assertEqual(assertivo["verdict"], "deterministic_match")
        self.assertFalse(assertivo["needs_vision"])


if __name__ == "__main__":
    unittest.main()
