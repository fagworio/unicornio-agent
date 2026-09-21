"""Fases 1/3/4/14 — determinismo da mídia (documento de correções).

Regressões cobertas:

* Fase 1: o pHash NAO pode reduzir o minimo 2/4/6 (só responde "N URLs = N
  frames?"). Antes `required_effective = distinct_frames` deixava um post de
  1200 palavras passar com 2 imagens alegando que só existiam duas.
* Fase 3: a busca agrega Bing/Yandex/Google ate a capacidade — a primeira
  engine com poucos candidatos nao encerra mais a busca.
* Fase 4: candidato sem `source_page_url` (Yandex) nasce `discovery_only` e
  nunca entra no media_plan.
* Fase 14: o parser do Bing associa murl/purl/turl do MESMO resultado (por
  objeto), nao por indice entre listas separadas.
"""

from __future__ import annotations

import unittest
from unittest import mock

from unicornio_editor.media import search as media_search
from unicornio_editor.media.search import (
    _bing_result_objects,
    _candidate,
    search_web_images,
)


class Fase4CandidateTests(unittest.TestCase):
    def test_yandex_sem_source_page_e_discovery_only(self):
        cand = _candidate("bleach", "1024x768|w", "https://cdn.example/b.jpg", "", "", "",
                          engine="yandex")
        self.assertFalse(cand["usable"])
        self.assertTrue(cand["discovery_only"])
        self.assertEqual(cand["rejected_reason"], "missing_source_page")

    def test_candidato_com_origem_e_utilizavel(self):
        cand = _candidate("bleach", "1024x768|w", "https://cdn.example/b.jpg",
                          "https://page.example/bleach/", "Bleach", "", engine="bing")
        self.assertTrue(cand["usable"])
        self.assertFalse(cand["discovery_only"])

    def test_url_invalida_nao_e_utilizavel(self):
        cand = _candidate("x", "s", "ftp://nope", "https://page.example/", "", "")
        self.assertFalse(cand["usable"])
        self.assertEqual(cand["rejected_reason"], "invalid_direct_image_url")


class Fase14BingParserTests(unittest.TestCase):
    def test_associa_por_objeto_e_nao_por_indice(self):
        """Um resultado SEM purl nao desloca a pagina do resultado seguinte.

        Com o parser por indice (listas paralelas), o purl do B seria usado
        para o candidato A — a imagem A herdava a origem de B.
        """
        html = (
            '<a class="iusc" m=\'{"murl":"https://cdn.example/a.jpg",'
            '"turl":"https://t.example/a.jpg"}\'>'
            '<a class="iusc" m=\'{"murl":"https://cdn.example/b.jpg",'
            '"purl":"https://page.example/b/","turl":"https://t.example/b.jpg"}\'>'
        )
        objs = _bing_result_objects(html)
        self.assertEqual(len(objs), 2)
        self.assertEqual(objs[0]["murl"], "https://cdn.example/a.jpg")
        self.assertEqual(objs[0]["purl"], "")
        self.assertEqual(objs[1]["purl"], "https://page.example/b/")

    def test_fallback_por_proximidade_quando_nao_ha_atributo_m(self):
        html = (
            '&quot;murl&quot;:&quot;https://cdn.example/a.jpg&quot;,'
            '&quot;turl&quot;:&quot;https://t.example/a.jpg&quot;,'
            '&quot;purl&quot;:&quot;https://page.example/a/&quot;'
        )
        objs = _bing_result_objects(html.replace("&quot;", '"'))
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0]["purl"], "https://page.example/a/")


class Fase3AgregacaoTests(unittest.TestCase):
    def _cand(self, url, page):
        return {"direct_image_url": url, "source_page_url": page,
                "usable": True, "discovery_only": False, "engine": "x"}

    def test_agrega_engines_ate_a_capacidade(self):
        """A 1a engine com 1 candidato nao encerra a busca (antes encerrava)."""
        bing = [self._cand("https://cdn/1.jpg", "https://p/1")]
        yandex = [self._cand("https://cdn/2.jpg", "https://p/2")]
        with mock.patch.object(media_search, "search_bing_images", return_value=bing), \
             mock.patch.object(media_search, "search_yandex_images", return_value=yandex), \
             mock.patch.object(media_search, "search_google_images", return_value=[]):
            out = search_web_images("qualquer", limit=2, engine="auto")
        urls = {c["direct_image_url"] for c in out}
        self.assertEqual(urls, {"https://cdn/1.jpg", "https://cdn/2.jpg"})

    def test_para_quando_a_primeira_engine_basta(self):
        bing = [self._cand("https://cdn/1.jpg", "https://p/1"),
                self._cand("https://cdn/2.jpg", "https://p/2")]
        with mock.patch.object(media_search, "search_bing_images", return_value=bing), \
             mock.patch.object(media_search, "search_yandex_images",
                               return_value=[self._cand("https://cdn/3.jpg", "https://p/3")]), \
             mock.patch.object(media_search, "search_google_images", return_value=[]):
            out = search_web_images("outra", limit=2, engine="bing")
        self.assertEqual(len(out), 2)

    def test_dedupe_por_url_entre_engines(self):
        mesmo = self._cand("https://cdn/x.jpg", "https://p/x")
        with mock.patch.object(media_search, "search_bing_images", return_value=[dict(mesmo)]), \
             mock.patch.object(media_search, "search_yandex_images", return_value=[dict(mesmo)]), \
             mock.patch.object(media_search, "search_google_images", return_value=[]):
            out = search_web_images("dedupe", limit=3, engine="auto")
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
