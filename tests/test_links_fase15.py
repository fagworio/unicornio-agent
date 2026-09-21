"""Fase 15 — links internos idempotentes (documento de correções)."""

import unittest

from unicornio_editor.internal_links import _canonical_url, add_internal_links


class Fase15IdempotenciaTests(unittest.TestCase):
    def test_tres_ocorrencias_geram_um_link(self):
        out = add_internal_links("<p>série, série e série</p>")
        self.assertEqual(out.lower().count("<a "), 1)

    def test_variacoes_do_mesmo_termo_usam_a_mesma_url(self):
        """série + séries + série de TV -> um único link /series/."""
        out = add_internal_links("<p>série, séries e série de TV</p>")
        self.assertEqual(out.lower().count("<a "), 1)
        self.assertIn("/series/", out)

    def test_conteudo_que_ja_tem_o_link_nao_ganha_outro(self):
        html = '<p><a href="https://www.unicorniohater.com.br/series/">série</a> e outra série.</p>'
        out = add_internal_links(html)
        self.assertEqual(out.lower().count("<a "), 1)

    def test_idempotente_dez_vezes(self):
        html = "<p>PlayStation 5 e Xbox e série e série.</p>"
        first = add_internal_links(html)
        atual = first
        for _ in range(10):
            atual = add_internal_links(atual)
        self.assertEqual(atual, first)

    def test_duplicata_existente_preserva_o_primeiro_link(self):
        html = (
            '<p><a href="/series/">Série</a></p><p><a href="/series/">série</a></p>'
        )
        out = add_internal_links(html)
        self.assertEqual(out.lower().count("<a "), 1)
        self.assertIn(">Série</a>", out)
        # o segundo virou texto simples
        self.assertIn("<p>série</p>", out)


class CanonicalUrlTests(unittest.TestCase):
    def test_variantes_apontam_para_a_mesma_url(self):
        esperado = _canonical_url("https://www.unicorniohater.com.br/series/")
        for variante in (
            "https://unicorniohater.com.br/series",
            "http://www.unicorniohater.com.br/series/",
            "https://www.unicorniohater.com.br//series//",
            "https://WWW.UnicornioHater.com.br/series",
        ):
            self.assertEqual(_canonical_url(variante), esperado, variante)

    def test_urls_distintas_nao_colidem(self):
        self.assertNotEqual(
            _canonical_url("https://www.unicorniohater.com.br/series/"),
            _canonical_url("https://www.unicorniohater.com.br/games/"),
        )


if __name__ == "__main__":
    unittest.main()
