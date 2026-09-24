"""Regressão: o href de resultado do Bing virou redirect ``/ck/a`` (2026).

O parser de PÁGINAS do resolver (`_buscador_padrao`) lia
``<h2><a href="https://pagina">``. Desde 2026 o Bing serve
``https://www.bing.com/ck/a?!&&p=<hash>&u=a1<base64url>&ntb=1`` — e o código
descartava qualquer link com "bing.com". Resultado: 100% dos resultados eram
jogados fora, o resolver encontrava ZERO páginas para qualquer query (medido:
0 páginas antes, 7-10 depois) e todo candidato do Yandex ficava ``unresolved``,
com a mídia presa em 0 aprovado.

A fixture é um trecho REAL (não editado) do HTML do Bing, congelado em
2026-09-24, com os hrefs ainda escapados (``&amp;``) como chegam do servidor.
"""

import unittest
from pathlib import Path
from unittest import mock

from unicornio_editor.media import search
from unicornio_editor.media import source_resolver

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures" / "bing_web_search" / "resultados_ck_a_20260924.html"
)


class DesembrulhoDoRedirectDoBingTests(unittest.TestCase):
    def test_desembrulha_o_href_real_da_fixture(self):
        html = FIXTURE.read_text(encoding="utf-8")
        hrefs = source_resolver.re.findall(r'href="(https://www\.bing\.com/ck/a[^"]+)"', html)
        self.assertGreaterEqual(len(hrefs), 1)
        alvos = [source_resolver.url_real_do_bing(href) for href in hrefs]
        self.assertIn("https://www.obsidian.net/", alvos)
        for alvo in alvos:
            self.assertTrue(alvo.startswith("http"))
            self.assertNotIn("bing.com", alvo)

    def test_url_comum_volta_intacta(self):
        self.assertEqual(
            source_resolver.url_real_do_bing("https://exemplo.com/materia"),
            "https://exemplo.com/materia",
        )

    def test_redirect_sem_alvo_volta_vazio(self):
        self.assertEqual(
            source_resolver.url_real_do_bing("https://www.bing.com/ck/a?!&amp;&amp;p=abc"),
            "",
        )

    def test_base64_invalido_volta_vazio(self):
        self.assertEqual(
            source_resolver.url_real_do_bing("https://www.bing.com/ck/a?u=a1@@@@"),
            "",
        )

    def test_entity_escapada_nao_engana_o_parser(self):
        """Sem desescapar o ``&amp;``, o parâmetro se chama ``amp;u`` e nada é lido."""
        href = (
            "https://www.bing.com/ck/a?!&amp;&amp;p=1"
            "&amp;u=a1aHR0cHM6Ly9leGVtcGxvLmNvbS9tYXRlcmlh&amp;ntb=1"
        )
        self.assertEqual(
            source_resolver.url_real_do_bing(href), "https://exemplo.com/materia"
        )


class BuscadorDePaginasDoResolverTests(unittest.TestCase):
    def test_buscador_le_a_fixture_e_nao_devolve_link_do_bing(self):
        html = FIXTURE.read_text(encoding="utf-8")
        with mock.patch.object(search, "_fetch", return_value=html):
            paginas = source_resolver._buscador_padrao("site:invenglobal.com Obsidian Entertainment")
        urls = [p["source_page_url"] for p in paginas]
        self.assertTrue(urls)
        for url in urls:
            self.assertTrue(url.startswith("http"))
            self.assertNotIn("bing.com", url)

    def test_antes_do_fix_o_resultado_seria_vazio(self):
        """Prova do bug: mantendo o filtro antigo, sobra zero página."""
        html = FIXTURE.read_text(encoding="utf-8")
        antigos = [
            trecho
            for trecho in source_resolver.re.findall(
                r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"', html
            )
            if "bing.com" not in trecho
        ]
        self.assertEqual(antigos, [])
        with mock.patch.object(search, "_fetch", return_value=html):
            self.assertTrue(source_resolver._buscador_padrao("qualquer coisa"))


if __name__ == "__main__":
    unittest.main()
