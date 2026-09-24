"""Saúde por engine: classificação da falha, breaker e telemetria.

Em 2026-09-24 ficou provado que o Google não entrega mais resultados no HTML
servido: qualquer endpoint (`udm=2`, `tbm=isch`, `images.google.com`, UA mobile)
devolve a página "ative o JavaScript" (~92 KB, `<noscript>` + redirect para
`/httpservice/retry/enablejs`). Como o parser devolvia 0 objetos e o breaker só
sabia "falhou", a engine entrava em cooldown de 12 min atrás de outro — 18
"falhas" seguidas — e saía da arquitetura sem deixar rastro na telemetria.

Cobre:
1. ``classify_failure`` separa transitório (network/rate-limit/http) de
   permanente (captcha/JS exigido/schema mudou) e de "sem resultado mesmo";
2. a fixture REAL congelada do interstitial é ``js_required`` — nunca
   ``rate_limited``;
3. motivo permanente NÃO abre cooldown e zera o contador transitório;
4. falha transitória continua abrindo o circuito (compatível com
   tests/test_media_engines.py);
5. o Google continua sendo tentado (arquitetura intacta) e o motivo aparece na
   telemetria com status/bytes/objetos/parser_version.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from unicornio_editor.media import search

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures" / "google_images" / "js_required_interstitial_20260924.html"
)

# Página de RESULTADOS no formato antigo: tem thumbnails/objetos, mas nenhuma das
# nossas chaves (tu/ou/ru/pt) — é o caso "o schema mudou".
HTML_SCHEMA_NOVO = (
    "<html><head><title>Google Search</title></head><body>"
    '<img src="https://encrypted-tbn0.gstatic.com/images?q=tbn:AAA">'
    '<script>AF_initDataCallback({key: "ds:7", data: ["https://exemplo.com/a.jpg"]});</script>'
    "</body></html>"
)
HTML_CAPTCHA = (
    "<html><body><h1>Our systems have detected unusual traffic</h1>"
    '<form action="/sorry/index"></form></body></html>'
)
HTML_SEM_RESULTADO = "<html><body><p>Nenhum resultado encontrado.</p></body></html>"


class _Resposta:
    """Resposta HTTP mínima para o mock de ``urlopen``."""

    def __init__(self, corpo: str, status: int = 200):
        self._corpo = corpo.encode()
        self.status = status

    def read(self, _limite: int = -1) -> bytes:
        return self._corpo

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ClassificacaoDeFalhaTests(unittest.TestCase):
    def test_interstitial_real_do_google_e_js_required(self):
        html = FIXTURE.read_text(encoding="utf-8")
        self.assertEqual(len(html.encode()), 92670)  # fixture congelada, byte a byte
        self.assertEqual(search._google_result_objects(html), [])
        self.assertEqual(
            search.classify_failure(html, http_status=200, objects_parsed=0),
            "js_required",
        )

    def test_schema_de_resultados_com_nossas_chaves_ausentes_e_drift(self):
        self.assertEqual(
            search.classify_failure(HTML_SCHEMA_NOVO, http_status=200, objects_parsed=0),
            "parser_schema_drift",
        )

    def test_captcha_nao_e_rate_limit(self):
        self.assertEqual(
            search.classify_failure(HTML_CAPTCHA, http_status=200, objects_parsed=0),
            "captcha",
        )

    def test_pagina_sem_resultado_nenhum(self):
        self.assertEqual(
            search.classify_failure(HTML_SEM_RESULTADO, http_status=200, objects_parsed=0),
            "no_results_legitimate",
        )

    def test_ok_quando_ha_objetos(self):
        self.assertEqual(
            search.classify_failure(HTML_SCHEMA_NOVO, http_status=200, objects_parsed=3),
            "ok",
        )

    def test_status_e_erros_de_http(self):
        self.assertEqual(search.classify_failure("", http_status=429), "rate_limited")
        self.assertEqual(search.classify_failure("", http_status=503), "rate_limited")
        self.assertEqual(search.classify_failure("", http_status=500), "http_error")
        self.assertEqual(search.classify_failure("", http_status=404), "http_error")
        self.assertEqual(search.classify_failure("", error="URLError: timed out"), "network_error")

    def test_fetch_report_classifica_sem_levantar(self):
        with mock.patch.object(
            search, "urlopen",
            side_effect=HTTPError("https://x", 429, "Too Many Requests", {}, None),
        ):
            html, relatorio = search._fetch_report("https://x", 5.0)
        self.assertIsNone(html)
        self.assertEqual(relatorio["failure_kind"], "rate_limited")
        self.assertEqual(relatorio["http_status"], 429)

        with mock.patch.object(search, "urlopen", side_effect=URLError("dns")):
            html, relatorio = search._fetch_report("https://x", 5.0)
        self.assertIsNone(html)
        self.assertEqual(relatorio["failure_kind"], "network_error")

        with mock.patch.object(search, "urlopen", return_value=_Resposta(HTML_SEM_RESULTADO)):
            html, relatorio = search._fetch_report("https://x", 5.0)
        self.assertIsNotNone(html)
        self.assertEqual(relatorio["http_status"], 200)
        self.assertEqual(relatorio["html_bytes"], len(HTML_SEM_RESULTADO.encode()))
        self.assertEqual(relatorio["failure_kind"], "network_error")  # ainda não finalizado

    def test_google_nao_entrega_nada_com_a_pagina_real(self):
        html = FIXTURE.read_text(encoding="utf-8")
        with mock.patch.object(search, "urlopen", return_value=_Resposta(html)):
            relatorio: dict = {}
            candidatos = search.search_google_images("qualquer", report=relatorio)
        self.assertEqual(candidatos, [])
        self.assertEqual(relatorio["failure_kind"], "js_required")
        self.assertEqual(relatorio["objects_parsed"], 0)
        self.assertEqual(relatorio["http_status"], 200)
        self.assertEqual(relatorio["parser_version"], search.PARSER_VERSION)


class BreakerNaoPuneMotivoPermanenteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self._antes = os.environ.get("UNICORNIO_ENGINE_STATE")
        os.environ["UNICORNIO_ENGINE_STATE"] = self.tmp.name
        Path(self.tmp.name).write_text(json.dumps({}), encoding="utf-8")

    def tearDown(self):
        if self._antes is None:
            os.environ.pop("UNICORNIO_ENGINE_STATE", None)
        else:
            os.environ["UNICORNIO_ENGINE_STATE"] = self._antes
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_degradada_registra_motivo_sem_cooldown(self):
        search.engine_degradada("google", "js_required")
        self.assertTrue(search.engine_disponivel("google"))
        estado = search.engines_status()["google"]
        self.assertEqual(estado["failure_kind"], "js_required")
        self.assertEqual(estado["blocked_until"], 0)
        self.assertEqual(estado["failures"], 0)
        self.assertTrue(estado["non_transient"])

    def test_degradada_limpa_o_cooldown_antigo_poluido(self):
        Path(self.tmp.name).write_text(
            json.dumps({"google": {"failures": 18, "blocked_until": 9_999_999_999.0}}),
            encoding="utf-8",
        )
        self.assertFalse(search.engine_disponivel("google"))
        search.engine_degradada("google", "js_required")
        self.assertTrue(search.engine_disponivel("google"))
        self.assertEqual(search.engines_status()["google"]["failures"], 0)

    def test_falha_transitoria_continua_abrindo_o_circuito(self):
        for _ in range(3):
            espera = search.engine_falhou("bing")
        self.assertGreaterEqual(espera, 600)
        self.assertFalse(search.engine_disponivel("bing"))
        self.assertEqual(search.engines_status()["bing"]["failures"], 3)

    def test_agregador_mantem_o_google_na_arquitetura(self):
        def _vazio_transitorio(query, **kwargs):
            return []

        def _vazio_js(query, report=None, **kwargs):
            if report is not None:
                report.update({
                    "http_status": 200, "html_bytes": 92670, "objects_parsed": 0,
                    "candidates": 0, "failure_kind": "js_required",
                    "parser_version": search.PARSER_VERSION,
                })
            return []

        relatorios: dict = {}
        with mock.patch.object(search, "search_bing_images", _vazio_transitorio), \
             mock.patch.object(search, "search_yandex_images", _vazio_transitorio), \
             mock.patch.object(search, "search_google_images", _vazio_js), \
             mock.patch.object(search.time, "sleep", lambda *_: None):
            resultado = search.search_web_images("qualquer coisa", limit=3, reports=relatorios)

        self.assertEqual(resultado, [])
        # Tentou as três engines — o Google não foi retirado da ordem.
        self.assertEqual(set(relatorios), {"bing", "yandex", "google"})
        self.assertEqual(relatorios["google"]["failure_kind"], "js_required")
        # O motivo permanente não virou cooldown...
        self.assertTrue(search.engine_disponivel("google"))
        self.assertEqual(search.engines_status()["google"]["failures"], 0)
        # ...e o transitório das outras duas continua contando.
        self.assertGreaterEqual(search.engines_status()["bing"]["failures"], 1)


class TelemetriaPorEngineTests(unittest.TestCase):
    def test_media_engine_health_registra_os_campos_pedidos(self):
        from unicornio_editor import cli

        with tempfile.TemporaryDirectory() as diretorio:
            root = Path(diretorio)
            cli._emit_engine_health(root, {
                "google": {
                    "http_status": 200, "html_bytes": 92670, "objects_parsed": 0,
                    "candidates": 0, "failure_kind": "js_required", "parser_version": 2,
                },
            }, query="Obsidian Entertainment")
            evento = json.loads(
                (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8").strip()
            )
        self.assertEqual(evento["event"], "media_engine_health")
        self.assertEqual(evento["engine"], "google")
        self.assertEqual(evento["http_status"], 200)
        self.assertEqual(evento["html_bytes"], 92670)
        self.assertEqual(evento["objects_parsed"], 0)
        self.assertEqual(evento["failure_kind"], "js_required")
        self.assertEqual(evento["parser_version"], 2)
        self.assertEqual(evento["query"], "Obsidian Entertainment")

    def test_batch_entrega_o_relatorio_de_cada_engine(self):
        def _com_relatorio(query, report=None, **kwargs):
            if report is not None:
                report.update({
                    "http_status": 200, "html_bytes": 10, "objects_parsed": 1,
                    "candidates": 1, "failure_kind": "ok",
                    "parser_version": search.PARSER_VERSION,
                })
            return [{
                "query": query, "direct_image_url": "https://cdn.x/a.jpg",
                "source_page_url": "https://pagina.x/a", "usable": True,
                "engine": "bing", "title": "t", "thumbnail_url": "",
                "size_filter": "1024x768|w", "discovery_only": False,
            }]

        with mock.patch.object(search, "search_bing_images", _com_relatorio), \
             mock.patch.object(search, "search_yandex_images", _com_relatorio), \
             mock.patch.object(search, "search_google_images", _com_relatorio):
            linhas = search.search_web_images_batch(["obra a"], limit=1)
            # limit alto: a busca não para na primeira engine e passa pelas três.
            linhas_duas = search.search_web_images_batch(["obra b"], limit=5)

        self.assertEqual(len(linhas), 1)
        self.assertIn("engine_reports", linhas[0])
        # Com limit=1 a parada por capacidade encerra na engine primária (a ordem
        # alterna por hash da query), mas o relatório dela está lá.
        primeira = linhas[0]["engine_reports"]
        self.assertEqual(len(primeira), 1)
        engine_primaria = next(iter(primeira))
        self.assertIn(engine_primaria, {"bing", "yandex"})
        self.assertEqual(primeira[engine_primaria]["failure_kind"], "ok")

        self.assertEqual(
            set(linhas_duas[0]["engine_reports"]), {"bing", "yandex", "google"}
        )
        self.assertEqual(
            linhas_duas[0]["engine_reports"]["bing"]["objects_parsed"], 1
        )


if __name__ == "__main__":
    unittest.main()
