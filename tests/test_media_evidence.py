"""Fases 7-11 — subject, contexto de origem e score determinístico."""

import unittest
from unittest import mock

from unicornio_editor.media import visual_hash
from unicornio_editor.media.evidence import (
    LIMIAR_LOCAL,
    source_context,
    LIMIAR_MATCH,
    dedupe_by_phash,
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


class DedupePhashTests(unittest.TestCase):
    """Fase 12: o mesmo frame em URLs diferentes é resolvido ANTES do upload."""

    def _cand(self, url, score=10):
        return {"direct_image_url": url, "evidence_score": score,
                "evidence": {"score": score, "verdict": "deterministic_match"}}

    def test_mesmo_frame_mantem_apenas_o_melhor(self):
        a, b, c = (self._cand("https://x/a.jpg", 21),
                   self._cand("https://x/b.jpg", 14),
                   self._cand("https://x/c.jpg", 9))
        with mock.patch.object(
            visual_hash, "image_hashes",
            return_value={"https://x/a.jpg": 100, "https://x/b.jpg": 102, "https://x/c.jpg": 90},
        ):
            mantidos, rejeitados = dedupe_by_phash([a, b, c], [])
        self.assertEqual([m["direct_image_url"] for m in mantidos],
                         ["https://x/a.jpg", "https://x/c.jpg"])
        self.assertEqual(len(rejeitados), 1)
        self.assertEqual(rejeitados[0]["evidence"]["verdict"], "duplicate_frame")
        self.assertEqual(rejeitados[0]["evidence"]["gate"], "diversity")

    def test_sem_hashes_suficientes_nao_descarta_nada(self):
        a, b = self._cand("https://x/a.jpg"), self._cand("https://x/b.jpg")
        with mock.patch.object(visual_hash, "image_hashes", return_value={}):
            mantidos, rejeitados = dedupe_by_phash([a, b], [])
        self.assertEqual(len(mantidos), 2)
        self.assertEqual(rejeitados, [])

    def test_um_unico_candidato_TAMBEM_recebe_phash(self):
        """P1 da auditoria: o singleton precisa do pHash.

        A contagem de capacidade usa pHash global ENTRE engines; sem hash ela
        cai para URL, e o MESMO frame servido por duas engines (URLs diferentes)
        contava como 2 frames distintos — a busca encerrava cedo e o dedupe
        final encontrava menos frames do que o alvo.
        """
        with mock.patch.object(
            visual_hash, "image_hashes", return_value={"https://x/a.jpg": "3f2a"}
        ) as m:
            mantidos, _ = dedupe_by_phash([self._cand("https://x/a.jpg")], [])
        m.assert_called_once()
        self.assertEqual(mantidos[0]["phash"], "3f2a")

    def test_cross_engine_mesmo_frame_conta_um(self):
        """Acceptance cross-engine: 2 engines, mesmo frame, URLs diferentes."""
        from unicornio_editor.media.search import search_web_images

        def bing(q, **kw):
            return [{"direct_image_url": "https://cdn.bing/a.jpg",
                     "source_page_url": "https://p.bing/a", "usable": True}]

        def yandex(q, **kw):
            return [{"direct_image_url": "https://cdn.ya/a.jpg",
                     "source_page_url": "https://p.ya/a", "usable": True}]

        def google(q, **kw):
            return []

        # as duas URLs servem o MESMO frame visual (hash idêntico)
        with mock.patch.object(
            visual_hash, "image_hashes",
            return_value={"https://cdn.bing/a.jpg": "3f2a", "https://cdn.ya/a.jpg": "3f2a"},
        ), mock.patch("unicornio_editor.media.search.search_bing_images", bing), \
           mock.patch("unicornio_editor.media.search.search_yandex_images", yandex), \
           mock.patch("unicornio_editor.media.search.search_google_images", google):

            def accept(novos):
                aprovados, _ = dedupe_by_phash(list(novos), [])
                return len({c.get("phash") or c.get("direct_image_url") for c in aprovados})

            # limit=2: se a URL fosse a chave, as 2 engines fechariam o alvo com
            # 1 frame real; com pHash a busca segue (0 aceitos distintos).
            out = search_web_images("x", limit=2, engine="auto", accept=accept)
        self.assertGreaterEqual(len(out), 2)  # consultou as duas engines


class EvidenciaLocalTests(unittest.TestCase):
    """Acceptance 2: página certa NÃO aprova imagem errada (avatar do autor)."""

    def test_avatar_do_autor_nao_vira_match_so_com_sinais_de_pagina(self):
        out = evidence_score(
            "metroid prime 4",
            filename="author-avatar.jpg",
            page_title="Nintendo apresenta Metroid Prime 4",
            og_title="Metroid Prime 4",
            page_url="https://www.nintendo.com/metroid-prime-4/",
            query="metroid prime 4",
        )
        # página diz tudo sobre Metroid (score alto), imagem não diz nada
        self.assertGreaterEqual(out["score"], LIMIAR_MATCH)
        self.assertEqual(out["local_score"], 0)
        self.assertEqual(out["verdict"], "ambiguous")

    def test_key_art_com_alt_original_e_match(self):
        out = evidence_score(
            "metroid prime 4",
            filename="metroid-prime-4-keyart.jpg",
            og_title="Metroid Prime 4",
            page_title="Metroid Prime 4: Beyond",
            alt_original="Metroid Prime 4 key art",
            page_url="https://www.nintendo.com/metroid-prime-4/",
            query="metroid prime 4",
        )
        self.assertGreaterEqual(out["local_score"], LIMIAR_LOCAL)
        self.assertEqual(out["verdict"], "deterministic_match")

    def test_filename_sozinho_ja_e_sinal_local(self):
        out = evidence_score(
            "pluto anime",
            filename="pluto-anime-keyart.jpg",
            page_title="Pluto",
            page_url="https://unicorniohater.com.br/pluto-anime/",
            query="pluto anime",
        )
        self.assertGreaterEqual(out["local_score"], LIMIAR_LOCAL)
        self.assertEqual(out["verdict"], "deterministic_match")


class ContextoImageLocalTests(unittest.TestCase):
    """P1 da auditoria: figcaption/heading precisam ser DA REGIÃO da imagem.

    Antes `source_context` pegava o primeiro <figcaption> e o primeiro <h1-4> da
    página inteira: numa matéria com uma key art em <figure> e um avatar do autor
    solto depois, o avatar herdava legenda E heading da key art (local_score 6
    sem nada local), furando o requisito local_score >= 4.
    """

    HTML = (
        "<html><head><title>Metroid Prime 4</title></head><body>"
        "<h1>Metroid Prime 4</h1>"
        '<figure><img src="https://cdn.x/metroid-keyart.jpg" alt="Metroid Prime 4 key art">'
        "<figcaption>Metroid Prime 4</figcaption></figure>"
        "<p>Texto da materia.</p>"
        '<img src="https://cdn.x/author-avatar.jpg">'
        "</body></html>"
    )

    def test_avatar_nao_herda_contexto_da_key_art(self):
        ctx = source_context(self.HTML, "https://cdn.x/author-avatar.jpg")
        self.assertNotIn("figcaption", ctx)
        self.assertNotIn("heading", ctx)
        self.assertEqual(ctx.get("filename"), "author-avatar.jpg")

    def test_key_art_recebe_o_proprio_contexto(self):
        ctx = source_context(self.HTML, "https://cdn.x/metroid-keyart.jpg")
        self.assertEqual(ctx.get("figcaption"), "Metroid Prime 4")
        self.assertEqual(ctx.get("heading"), "Metroid Prime 4")
        self.assertEqual(ctx.get("alt_original"), "Metroid Prime 4 key art")

    def test_avatar_nao_vira_deterministic_match(self):
        ctx = source_context(self.HTML, "https://cdn.x/author-avatar.jpg")
        out = evidence_score(
            "metroid prime 4",
            filename=ctx.get("filename", ""),
            page_title=ctx.get("page_title", ""),
            og_title=ctx.get("og_title", ""),
            alt_original=ctx.get("alt_original", ""),
            figcaption=ctx.get("figcaption", ""),
            heading=ctx.get("heading", ""),
            page_url="https://www.nintendo.com/metroid-prime-4/",
            query="metroid prime 4",
        )
        self.assertEqual(out["local_score"], 0)
        self.assertEqual(out["verdict"], "ambiguous")


if __name__ == "__main__":
    unittest.main()
