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
5. O plano separa providers obrigatórios e opcionais; Google Browser/Playwright
   legado nunca é executado e falhas opcionais não contaminam a prova core.
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
        search.engine_degradada("google_images", "js_required")
        self.assertTrue(search.engine_disponivel("google_images"))
        estado = search.engines_status()["google_images"]
        self.assertEqual(estado["failure_kind"], "js_required")
        self.assertEqual(estado["blocked_until"], 0)
        self.assertEqual(estado["failures"], 0)
        self.assertTrue(estado["non_transient"])

    def test_degradada_limpa_o_cooldown_antigo_poluido(self):
        Path(self.tmp.name).write_text(
            json.dumps({"google": {"failures": 18, "blocked_until": 9_999_999_999.0}}),
            encoding="utf-8",
        )
        # O estado legado não é reutilizado pela nova chave operacional.
        self.assertTrue(search.engine_disponivel("google_images"))
        search.engine_degradada("google_images", "js_required")
        self.assertTrue(search.engine_disponivel("google_images"))
        self.assertEqual(search.engines_status()["google_images"]["failures"], 0)

    def test_falha_transitoria_continua_abrindo_o_circuito(self):
        for _ in range(3):
            espera = search.engine_falhou("bing_images")
        self.assertGreaterEqual(espera, 600)
        self.assertFalse(search.engine_disponivel("bing_images"))
        self.assertEqual(search.engines_status()["bing_images"]["failures"], 3)

    def test_google_html_js_required_permanece_falha_quando_opt_in(self):
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
        with mock.patch.dict(os.environ, {
            "EDITOR_MEDIA_SEARCH_PROVIDERS": "bing,yandex,google",
            "EDITOR_GOOGLE_BROWSER_ENABLED": "false",
        }), \
             mock.patch.object(search, "search_bing_images", _vazio_transitorio), \
             mock.patch.object(search, "search_yandex_images", _vazio_transitorio), \
             mock.patch.object(search, "search_google_images", _vazio_js), \
             mock.patch.object(search.time, "sleep", lambda *_: None):
            resultado = search.search_web_images("qualquer coisa", limit=3, reports=relatorios)

        self.assertEqual(resultado, [])
        self.assertEqual(set(relatorios), {"bing", "yandex", "google"})
        self.assertEqual(relatorios["google"]["failure_kind"], "js_required")
        # O motivo permanente não virou cooldown...
        self.assertTrue(search.engine_disponivel("google_images"))
        self.assertEqual(search.engines_status()["google_images"]["failures"], 0)
        # ...e o transitório das outras duas continua contando.
        self.assertGreaterEqual(search.engines_status()["bing_images"]["failures"], 1)


class TelemetriaPorEngineTests(unittest.TestCase):
    def test_auto_default_strategy_is_ddg_bing_yandex_google(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            strategy = search.search_engine_strategy("auto")

        self.assertEqual(
            strategy["execution_engines"], ["duckduckgo", "bing", "yandex", "google"]
        )
        self.assertEqual(strategy["expected_engines"], ["bing", "yandex"])
        self.assertEqual(strategy["optional_engines"], ["duckduckgo", "google"])

    def test_default_plan_orders_fallbacks_but_requires_only_bing_yandex(self):
        def _legitimate_empty(_query, report=None, **_kwargs):
            if report is not None:
                report.update({"failure_kind": "no_results_legitimate", "candidates": 0})
            return []

        def _ddg_error(_query, report=None, **_kwargs):
            if report is not None:
                report.update({"failure_kind": "provider_error", "error": "DDGSException"})
            return []

        def _google_js(_query, report=None, **_kwargs):
            if report is not None:
                report.update({"failure_kind": "js_required", "candidates": 0})
            return []

        with mock.patch.dict(os.environ, {
            "EDITOR_MEDIA_SEARCH_PROVIDERS": "duckduckgo,bing,yandex,google",
            "EDITOR_MEDIA_SEARCH_REQUIRED_PROVIDERS": "bing,yandex",
        }), mock.patch(
            "unicornio_editor.media.duckduckgo_images.search_duckduckgo_images",
            _ddg_error,
        ), \
             mock.patch.object(search, "search_bing_images", _legitimate_empty), \
             mock.patch.object(search, "search_yandex_images", _legitimate_empty), \
             mock.patch.object(search, "search_google_images", _google_js):
            strategy = search.search_engine_strategy("auto")
            result = search.search_web_images_batch(["verified plan"], limit=10)[0]

        self.assertEqual(
            strategy["execution_engines"], ["duckduckgo", "bing", "yandex", "google"]
        )
        self.assertEqual(strategy["expected_engines"], ["bing", "yandex"])
        self.assertEqual(strategy["optional_engines"], ["duckduckgo", "google"])
        self.assertEqual(result["completion_reason"], "EXHAUSTED_VERIFIED")
        self.assertEqual(result["engine_summary"]["expected_engines"], ["bing", "yandex"])
        self.assertEqual(result["engine_summary"]["configured_engines"], strategy["execution_engines"])
        self.assertEqual(result["engine_summary"]["optional_provider_failures"], 2)
        self.assertEqual(
            result["engine_summary"]["optional_failure_engines"], ["duckduckgo", "google"]
        )

    def test_required_provider_must_be_in_execution_plan(self):
        with mock.patch.dict(os.environ, {
            "EDITOR_MEDIA_SEARCH_PROVIDERS": "duckduckgo,bing,yandex",
            "EDITOR_MEDIA_SEARCH_REQUIRED_PROVIDERS": "bing,google",
        }):
            strategy = search.search_engine_strategy("auto")

        self.assertFalse(strategy["valid"])
        self.assertEqual(strategy["execution_engines"], [])
        self.assertEqual(strategy["invalid_engines"], ["google"])

    def test_auto_default_executa_e_exige_so_bing_yandex(self):
        def _legitimate_empty(_query, report=None, **_kwargs):
            if report is not None:
                report.update({"failure_kind": "no_results_legitimate", "candidates": 0})
            return []

        with mock.patch.dict(os.environ, {
            "EDITOR_MEDIA_SEARCH_PROVIDERS": "bing,yandex",
            "EDITOR_GOOGLE_BROWSER_ENABLED": "false",
        }), mock.patch.object(search, "search_bing_images", side_effect=_legitimate_empty) as bing, \
             mock.patch.object(search, "search_yandex_images", side_effect=_legitimate_empty) as yandex, \
             mock.patch.object(search, "search_google_images", side_effect=AssertionError("Google HTML executed")):
            result = search.search_web_images_batch(["verified empty"], limit=10)[0]

        self.assertEqual(result["engine_reports"].keys(), {"bing", "yandex"})
        self.assertEqual(result["engine_summary"]["expected_engines"], ["bing", "yandex"])
        self.assertEqual(result["completion_reason"], "EXHAUSTED_VERIFIED")
        self.assertTrue(result["provider_config_valid"])
        bing.assert_called_once()
        yandex.assert_called_once()

    def test_browser_disabled_is_not_executed_nor_expected(self):
        def _legitimate_empty(_query, report=None, **_kwargs):
            if report is not None:
                report.update({"failure_kind": "no_results_legitimate", "candidates": 0})
            return []

        with mock.patch.dict(os.environ, {
            "EDITOR_MEDIA_SEARCH_PROVIDERS": "google_browser,bing,yandex",
            "EDITOR_GOOGLE_BROWSER_ENABLED": "false",
        }), mock.patch.object(search, "search_bing_images", _legitimate_empty), \
             mock.patch.object(search, "search_yandex_images", _legitimate_empty):
            result = search.search_web_images_batch(["browser disabled"], limit=10)[0]

        self.assertEqual(result["engine_summary"]["expected_engines"], ["bing", "yandex"])
        self.assertIn("google_browser", result["engine_summary"]["disabled_engines"])
        self.assertNotIn("google_browser", result["engine_reports"])
        self.assertEqual(result["completion_reason"], "EXHAUSTED_VERIFIED")

    def test_browser_legacy_flag_true_is_ignored_when_supported_providers_exist(self):
        def _legitimate_empty(_query, report=None, **_kwargs):
            if report is not None:
                report.update({"failure_kind": "no_results_legitimate", "candidates": 0})
            return []

        with mock.patch.dict(os.environ, {
            "EDITOR_MEDIA_SEARCH_PROVIDERS": "google_browser,bing,yandex",
            "EDITOR_GOOGLE_BROWSER_ENABLED": "true",
        }), mock.patch.object(search, "search_bing_images", side_effect=_legitimate_empty) as bing, \
             mock.patch.object(search, "search_yandex_images", side_effect=_legitimate_empty) as yandex:
            strategy = search.search_engine_strategy("auto")
            result = search.search_web_images_batch(["legacy browser flag"], limit=10)[0]

        self.assertTrue(strategy["valid"])
        self.assertEqual(strategy["expected_engines"], ["bing", "yandex"])
        self.assertIn("google_browser", strategy["disabled_engines"])
        self.assertNotIn("google_browser", result["engine_reports"])
        self.assertEqual(result["completion_reason"], "EXHAUSTED_VERIFIED")
        bing.assert_called_once()
        yandex.assert_called_once()

    def test_retired_browser_only_configuration_stays_incomplete(self):
        with mock.patch.dict(os.environ, {
            "EDITOR_MEDIA_SEARCH_PROVIDERS": "google_browser",
            "EDITOR_GOOGLE_BROWSER_ENABLED": "true",
        }):
            strategy = search.search_engine_strategy("auto")

        self.assertFalse(strategy["valid"])
        self.assertEqual(strategy["expected_engines"], [])
        self.assertIn("google_browser", strategy["disabled_engines"])
        self.assertEqual(strategy["invalid_engines"], ["no_supported_engines"])

    def test_optional_google_js_failure_does_not_contaminate_core_exhaustion(self):
        def _legitimate_empty(_query, report=None, **_kwargs):
            if report is not None:
                report.update({"failure_kind": "no_results_legitimate", "candidates": 0})
            return []

        def _requires_js(_query, report=None, **_kwargs):
            if report is not None:
                report.update({"failure_kind": "js_required", "candidates": 0})
            return []

        with mock.patch.dict(os.environ, {
            "EDITOR_MEDIA_SEARCH_PROVIDERS": "bing,yandex,google",
            "EDITOR_MEDIA_SEARCH_REQUIRED_PROVIDERS": "bing,yandex",
        }), mock.patch.object(search, "search_bing_images", _legitimate_empty), \
             mock.patch.object(search, "search_yandex_images", _legitimate_empty), \
             mock.patch.object(search, "search_google_images", _requires_js):
            result = search.search_web_images_batch(["Google JS failure"], limit=10)[0]

        self.assertEqual(result["engine_summary"]["expected_engines"], ["bing", "yandex"])
        self.assertEqual(result["engine_reports"]["google"]["failure_kind"], "js_required")
        self.assertEqual(result["completion_reason"], "EXHAUSTED_VERIFIED")
        self.assertTrue(result["engine_summary"]["provider_strategy_complete"])
        self.assertEqual(result["engine_summary"]["optional_provider_failures"], 1)
        self.assertEqual(result["engine_summary"]["optional_failure_engines"], ["google"])

    def test_google_js_failure_blocks_exhaustion_when_explicitly_required(self):
        def _legitimate_empty(_query, report=None, **_kwargs):
            if report is not None:
                report.update({"failure_kind": "no_results_legitimate", "candidates": 0})
            return []

        def _requires_js(_query, report=None, **_kwargs):
            if report is not None:
                report.update({"failure_kind": "js_required", "candidates": 0})
            return []

        with mock.patch.dict(os.environ, {
            "EDITOR_MEDIA_SEARCH_PROVIDERS": "bing,yandex,google",
            "EDITOR_MEDIA_SEARCH_REQUIRED_PROVIDERS": "google",
        }), mock.patch.object(search, "search_bing_images", _legitimate_empty), \
             mock.patch.object(search, "search_yandex_images", _legitimate_empty), \
             mock.patch.object(search, "search_google_images", _requires_js):
            result = search.search_web_images_batch(["required Google JS failure"], limit=10)[0]

        self.assertEqual(result["engine_summary"]["expected_engines"], ["google"])
        self.assertEqual(result["completion_reason"], "INCOMPLETE_PROVIDER")
        self.assertEqual(result["engine_summary"]["required_failure_engines"], ["google"])
        self.assertFalse(result["engine_summary"]["provider_strategy_complete"])

    def test_unknown_configured_provider_makes_strategy_unverifiable(self):
        with mock.patch.dict(os.environ, {"EDITOR_MEDIA_SEARCH_PROVIDERS": "bing,yandex,googl"}):
            strategy = search.search_engine_strategy("auto")
        self.assertFalse(strategy["valid"])
        self.assertEqual(strategy["expected_engines"], [])
        self.assertEqual(strategy["invalid_engines"], ["googl"])

    def test_resumo_por_consulta_preserva_denominador_e_motivos(self):
        resumo = search.summarize_engine_reports(
            {
                "google_browser": {
                    "failure_kind": "google_captcha",
                    "candidates": 0,
                    "objects_parsed": 0,
                },
                "bing": {
                    "failure_kind": "no_results_legitimate",
                    "candidates": 0,
                    "objects_parsed": 0,
                },
                "yandex": {
                    "failure_kind": "js_required",
                    "candidates": 2,
                    "objects_parsed": 3,
                },
            },
            expected_engines=["google_browser", "bing", "yandex", "google"],
        )

        self.assertEqual(resumo["reported_engines"], ["google_browser", "bing", "yandex"])
        self.assertEqual(resumo["completed_engines"], ["bing"])
        self.assertEqual(resumo["missing_engines"], ["google"])
        self.assertEqual(resumo["provider_failures"], 2)
        self.assertEqual(resumo["transient_failures"], 0)
        self.assertEqual(
            resumo["failure_kinds"],
            {"google_captcha": 1, "js_required": 1, "no_results_legitimate": 1},
        )
        self.assertEqual(resumo["candidates_discovered"], 2)
        self.assertEqual(resumo["objects_parsed"], 3)

    def test_resumo_nao_trata_fallbacks_nao_tentados_como_falha(self):
        resumo = search.summarize_engine_reports(
            {
                "google_browser": {"failure_kind": "google_browser_unavailable"},
                "bing": {"failure_kind": "ok", "candidates": 3},
            },
            expected_engines=["google_browser", "bing", "yandex", "google"],
            completion_reason="TARGET_REACHED",
        )

        self.assertTrue(resumo["capacity_reached"])
        self.assertFalse(resumo["provider_strategy_complete"])
        self.assertEqual(resumo["completion_reason"], "TARGET_REACHED")
        self.assertEqual(resumo["not_attempted_engines"], ["yandex", "google"])
        self.assertEqual(resumo["unresolved_engines"], [])
        # A reported browser failure remains visible; only the unattempted
        # fallbacks stop being interpreted as failures.
        self.assertEqual(resumo["provider_failures"], 1)

    def test_media_engine_health_registra_os_campos_pedidos(self):
        from unicornio_editor import cli

        with tempfile.TemporaryDirectory() as diretorio:
            root = Path(diretorio)
            cli._emit_engine_health(root, {
                "google": {
                    "http_status": 200, "html_bytes": 92670, "objects_parsed": 0,
                    "candidates": 0, "failure_kind": "js_required", "parser_version": 2,
                    "final_url": "https://images.google.test/search?signature=private-token",
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
        self.assertEqual(evento["final_domain"], "images.google.test")
        self.assertNotIn("private-token", json.dumps(evento))

    def test_media_engine_health_registra_estagio_ddgs_sem_urls_ou_tokens(self):
        from unicornio_editor import cli

        with tempfile.TemporaryDirectory() as diretorio:
            root = Path(diretorio)
            cli._emit_engine_health(root, {
                "duckduckgo": {
                    "backend": "duckduckgo", "upstream_provider": "bing",
                    "http_status": 202, "failure_kind": "provider_error",
                    "failure_stage": "vqd_bootstrap", "outcome": "provider_unavailable",
                    "results_verified": False,
                    "request_observations": [
                        {
                            "stage": "vqd_bootstrap", "http_status": 202,
                            "url": "https://duckduckgo.com/?vqd=signed-secret",
                            "body": "captcha token secret",
                        },
                        {"stage": "attacker-controlled", "http_status": 200},
                    ],
                },
            })
            evento = json.loads(
                (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8").strip()
            )

        self.assertEqual(evento["failure_stage"], "vqd_bootstrap")
        self.assertEqual(evento["outcome"], "provider_unavailable")
        self.assertFalse(evento["results_verified"])
        self.assertEqual(evento["request_observations"], [
            {"stage": "vqd_bootstrap", "http_status": 202},
        ])
        self.assertNotIn("duckduckgo.com", json.dumps(evento))
        self.assertNotIn("signed-secret", json.dumps(evento))
        self.assertNotIn("captcha token", json.dumps(evento))

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

        with mock.patch.dict(os.environ, {
            "EDITOR_MEDIA_SEARCH_PROVIDERS": "bing,yandex,google",
        }), mock.patch.object(search, "search_bing_images", _com_relatorio), \
             mock.patch.object(search, "search_yandex_images", _com_relatorio), \
             mock.patch.object(search, "search_google_images", _com_relatorio):
            linhas = search.search_web_images_batch(["obra a"], limit=1)
            # limit alto: a busca não para na primeira engine e passa pelas três.
            linhas_duas = search.search_web_images_batch(["obra b"], limit=5)

        self.assertEqual(len(linhas), 1)
        self.assertIn("engine_reports", linhas[0])
        # A parada por capacidade encerra na engine primária sem tentar
        # providers que deixaram de ser necessários.
        primeira = linhas[0]["engine_reports"]
        self.assertEqual(len(primeira), 1)
        engine_primaria = next(iter(primeira))
        self.assertIn(engine_primaria, {"bing", "yandex"})
        self.assertEqual(primeira[engine_primaria]["failure_kind"], "ok")

        self.assertEqual(
            set(linhas_duas[0]["engine_reports"]),
            {"bing", "yandex", "google"},
        )
        self.assertEqual(
            linhas_duas[0]["engine_reports"]["bing"]["objects_parsed"], 1
        )
        self.assertIn("engine_summary", linhas_duas[0])
        self.assertEqual(linhas_duas[0]["engine_summary"]["provider_failures"], 0)
        self.assertEqual(linhas_duas[0]["engine_summary"]["failure_kinds"].get("ok"), 3)
        self.assertEqual(linhas[0]["completion_reason"], "TARGET_REACHED")
        self.assertEqual(
            linhas[0]["engine_summary"]["not_attempted_engines"],
            [
                name for name in ("bing", "yandex", "google")
                if name not in linhas[0]["engine_reports"]
            ],
        )

    def test_batch_nao_marca_target_com_candidatos_inutilizaveis(self):
        candidatos = [
            {"direct_image_url": "https://cdn.test/a.jpg", "usable": False},
            {"direct_image_url": "https://cdn.test/b.jpg", "usable": False},
        ]

        def _search_without_usable(_query, reports=None, **_kwargs):
            reports.update({"bing": {"failure_kind": "ok", "candidates": 2}})
            return candidatos

        with mock.patch.object(
            search,
            "search_web_images",
            side_effect=_search_without_usable,
        ):
            linhas = search.search_web_images_batch(["sem origem"], limit=2)

        self.assertEqual(linhas[0]["completion_reason"], "INCOMPLETE_PROVIDER")
        self.assertFalse(linhas[0]["engine_summary"]["capacity_reached"])


if __name__ == "__main__":
    unittest.main()
