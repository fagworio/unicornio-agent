"""Circuit breaker por engine + desambiguação do subject por item."""

import json
import os
import tempfile
import unittest

from unicornio_editor.media import search
from unicornio_editor.media.evidence import item_query, tipo_de_conteudo


class DesambiguacaoTests(unittest.TestCase):
    def test_item_pluto_ganha_o_tipo_do_artigo(self):
        self.assertEqual(item_query("Pluto", "10 melhores animes"), "Pluto anime")
        self.assertEqual(item_query("Dune", "os melhores filmes"), "Dune filme")
        self.assertEqual(item_query("Metroid Prime 4", "novos jogos de 2026"), "Metroid Prime 4 game")

    def test_sem_contexto_nao_inventa_termo(self):
        self.assertEqual(item_query("Pluto", ""), "Pluto")
        self.assertEqual(item_query("Pluto", "noticias do portal"), "Pluto")

    def test_nao_duplica_termo_ja_presente(self):
        self.assertEqual(item_query("Pluto anime", "10 melhores animes"), "Pluto anime")

    def test_tipo_de_conteudo_reconhece_plural_e_marca(self):
        self.assertEqual(tipo_de_conteudo("ranking de animes"), "anime")
        self.assertEqual(tipo_de_conteudo("as melhores HQs da Marvel"), "quadrinho")


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self._antes = os.environ.get("UNICORNIO_ENGINE_STATE")
        os.environ["UNICORNIO_ENGINE_STATE"] = self.tmp.name
        with open(self.tmp.name, "w") as fh:
            json.dump({}, fh)

    def tearDown(self):
        if self._antes is None:
            os.environ.pop("UNICORNIO_ENGINE_STATE", None)
        else:
            os.environ["UNICORNIO_ENGINE_STATE"] = self._antes
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_backoff_cresce_e_terceira_falha_abre_o_circuito(self):
        self.assertTrue(search.engine_disponivel("bing"))
        e1 = search.engine_falhou("bing")
        self.assertGreater(e1, 0)
        self.assertLessEqual(e1, 8.0)
        self.assertFalse(search.engine_disponivel("bing"))
        e2 = search.engine_falhou("bing")
        self.assertGreater(e2, 8.0)
        e3 = search.engine_falhou("bing")
        self.assertGreaterEqual(e3, 600)  # cooldown 10-15 min
        self.assertFalse(search.engine_disponivel("bing"))

    def test_sucesso_fecha_o_circuito(self):
        search.engine_falhou("yandex")
        search.engine_falhou("yandex")
        search.engine_falhou("yandex")
        self.assertFalse(search.engine_disponivel("yandex"))
        search.engine_ok("yandex")
        self.assertTrue(search.engine_disponivel("yandex"))
        self.assertEqual(search.engines_status().get("yandex"), None)

    def test_engines_sao_independentes(self):
        search.engine_falhou("bing")
        search.engine_falhou("bing")
        search.engine_falhou("bing")
        self.assertFalse(search.engine_disponivel("bing"))
        self.assertTrue(search.engine_disponivel("yandex"))


if __name__ == "__main__":
    unittest.main()
