"""Testes de regressão da sexta rodada (medição correta antes de congelar).

1. `first_pass_attempts` conta SÓ primeiras tentativas (READY na 2ª não é
   primeira tentativa) e existe `first_pass_success_rate` de verdade;
2. `decision`/`score_gap` da telemetria vêm do LEDGER pelo `decision_id` (o texto
   do plano não pode mentir para a medição);
3. cobertura da atribuição (resolved/missing/invalid/mixed) + taxa;
4. Visão DIRETA do pipeline entra no grand total (tokens e requests) — sem somar
   `cached_tokens` de novo (prompt_tokens já os inclui);
5. requests/tokens em camadas: main, auxiliar, visão direta e total.
"""

import datetime
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _ts(segundos_atras: int = 0) -> str:
    # Timestamp RELATIVO: o guard filtra por janela de 24h e um `ts` fixo faz o
    # teste expirar quando o dia vira (foi o CI vermelho de 23/09).
    momento = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=segundos_atras
    )
    return momento.isoformat(timespec="seconds")

from unicornio_editor.observability import (
    append_telemetry,
    read_telemetry_summary,
    record_media_decision,
)
from unicornio_editor.session_metrics import session_metrics

CRON_ENV = {"UNICORNIO_RUN_SOURCE": "cron",
            "HERMES_SESSION_ID": "cron_9e39343dc6f5_20260922_090524"}


def _guard():
    import importlib.util

    caminho = Path(__file__).resolve().parents[1] / "hermes" / "cost_guard.py"
    spec = importlib.util.spec_from_file_location("cost_guard_r6", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class FirstPassAttemptsTests(unittest.TestCase):
    """READY na 2ª tentativa NÃO é primeira tentativa."""

    def test_ready_na_segunda_tentativa_nao_infla_o_denominador(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                # 1ª tentativa: bloqueado.
                append_telemetry(root, "apply_blocked", post_id=9, decision="auto",
                                 first_pass=True, failure_reasons=["imagens_no_corpo"])
                # 2ª tentativa: ficou READY (não é primeira tentativa).
                append_telemetry(root, "apply_ready", post_id=9, decision="auto",
                                 first_pass=False, attempts=2)
                resumo = read_telemetry_summary(root)
        qualidade = resumo["decision_quality"]["auto"]
        self.assertEqual(qualidade["first_pass_attempts"], 1)
        self.assertEqual(qualidade["first_pass_blocked"], 1)
        self.assertEqual(qualidade["first_pass_ready"], 0)
        self.assertEqual(qualidade["first_pass_success_rate"], 0.0)
        producao = resumo["production"]
        self.assertEqual(producao["first_pass_attempts"], 1)
        self.assertEqual(producao["first_pass_success_rate"], 0.0)
        # Dos READY, 0 foram de primeira (o único READY veio na 2ª tentativa).
        self.assertEqual(producao["ready_first_pass_share"], 0.0)

    def test_primeira_tentativa_bem_sucedida_tem_taxa_1(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "apply_ready", post_id=10, decision="choose",
                                 first_pass=True, attempts=1)
                resumo = read_telemetry_summary(root)
        self.assertEqual(resumo["decision_quality"]["choose"]["first_pass_success_rate"], 1.0)
        self.assertEqual(resumo["production"]["first_pass_success_rate"], 1.0)


class VisionDirectGrandTotalTests(unittest.TestCase):
    """Visão direta fecha o grand total (fora do accounting do Hermes)."""

    def _telemetria(self, root: Path) -> Path:
        caminho = root / "telemetry.jsonl"
        linhas = [
            {"event": "vision_api_request", "detail": "low", "run_source": "cron",
             "cron_job_id": "9e39343dc6f5", "input_tokens": 1000, "cached_tokens": 400,
             "output_tokens": 20, "ts": _ts(3)},
            {"event": "vision_api_request", "detail": "high", "run_source": "cron",
             "cron_job_id": "9e39343dc6f5", "input_tokens": 13_000, "cached_tokens": 0,
             "output_tokens": 25, "ts": _ts(2)},
            # Erro conta como requisição (gastou a chamada).
            {"event": "vision_api_request", "detail": "low", "run_source": "cron",
             "cron_job_id": "9e39343dc6f5", "error": "HTTP 500",
             "ts": _ts(1)},
            # Execução MANUAL não entra.
            {"event": "vision_api_request", "detail": "low", "run_source": "manual",
             "input_tokens": 999_999, "ts": _ts(0)},
        ]
        caminho.write_text("\n".join(json.dumps(linha) for linha in linhas), encoding="utf-8")
        return caminho

    def test_vision_direct_usage_filtra_origem_e_job(self):
        modulo = _guard()
        with tempfile.TemporaryDirectory() as directory:
            caminho = self._telemetria(Path(directory))
            dados = modulo.vision_direct_usage(
                caminho, hours=24, job_id="9e39343dc6f5", run_source="cron"
            )
        self.assertEqual(dados["requests"], 3)
        self.assertEqual(dados["prompt_tokens"], 14_000)
        self.assertEqual(dados["cached_tokens"], 400)
        self.assertEqual(dados["output_tokens"], 45)
        self.assertEqual(dados["errors"], 1)
        self.assertTrue(dados["measurable"])

    def test_grand_total_do_guard_inclui_visao_direta(self):
        modulo = _guard()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetria = self._telemetria(root)
            banco = root / "state.db"
            db = sqlite3.connect(banco)
            db.execute(
                "CREATE TABLE sessions (id TEXT, source TEXT, started_at INTEGER, "
                "api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
                "cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
                "reasoning_tokens INTEGER, estimated_cost_usd REAL)"
            )
            db.execute(
                "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT, "
                "api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
                "cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
                "reasoning_tokens INTEGER, estimated_cost_usd REAL)"
            )
            agora = int(time.time())
            db.execute(
                "INSERT INTO sessions VALUES ('cron_9e39343dc6f5_20260922_090524','cron',?,"
                "40,1000,200,9000,0,0,0.20)",
                (agora,),
            )
            db.execute(
                "INSERT INTO session_model_usage VALUES "
                "('cron_9e39343dc6f5_20260922_090524','vision','compression',2,1000,10,0,0,0,0.01)"
            )
            db.commit()
            db.close()
            med = modulo.usage_measurement_in_last_24h(
                banco, "9e39343dc6f5", str(root), telemetry_path=telemetria
            )
        # Camadas separadas.
        self.assertEqual(med["main_prompt_tokens"], 10_000)
        self.assertEqual(med["aux_prompt_tokens"], 1_000)
        self.assertEqual(med["direct_vision_prompt_tokens"], 14_000)
        # cached NÃO é somado de novo (prompt_tokens já o inclui).
        self.assertEqual(med["grand_total_prompt_tokens"], 25_000)
        self.assertEqual(med["prompt_tokens"], 25_000)
        # Requests: main + aux + visão direta.
        self.assertEqual(med["main_requests"], 40)
        self.assertEqual(med["aux_requests"], 2)
        self.assertEqual(med["direct_vision_requests"], 3)
        self.assertEqual(med["grand_total_requests"], 45)
        self.assertEqual(med["requests"], 45)
        # Custo: só as camadas do Hermes (visão direta não tem preço).
        self.assertEqual(med["cost_usd"], 0.21)
        self.assertIsNone(med["cost_direct_vision_usd"])

    def test_limite_de_requests_usa_o_total(self):
        modulo = _guard()
        medidos = {"cost_usd": 0.21, "requests": 45, "prompt_tokens": 25_000}
        self.assertEqual(
            modulo._decision({"requests": 45.0}, medidos), ("block", "requests")
        )
        self.assertEqual(
            modulo._decision({"requests": 46.0}, medidos), ("allow", "within_budget")
        )


class SessionMetricsDirectVisionTests(unittest.TestCase):
    """`telemetry --sessions` expõe a camada direta e o total observado."""

    def test_observed_grand_total_soma_visao_direta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            banco = root / "state.db"
            db = sqlite3.connect(banco)
            db.execute(
                "CREATE TABLE sessions (id TEXT, source TEXT, started_at INTEGER, "
                "api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
                "cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
                "reasoning_tokens INTEGER, estimated_cost_usd REAL, tool_call_count INTEGER)"
            )
            db.execute(
                "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT, "
                "api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
                "cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
                "reasoning_tokens INTEGER, estimated_cost_usd REAL)"
            )
            db.execute(
                "INSERT INTO sessions VALUES ('cron_9e39343dc6f5_20260922_090524','cron',?,"
                "10,1000,100,0,0,0,0.05,10)",
                (int(time.time()),),
            )
            db.commit()
            db.close()
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "apply_ready", post_id=1, first_pass=True)
                append_telemetry(root, "vision_api_request", detail="low",
                                 input_tokens=500, cached_tokens=100, output_tokens=10)
                metricas = session_metrics(
                    root, state_db=banco, job_id="9e39343dc6f5",
                    project_root=str(root), hours=24,
                )
        self.assertEqual(metricas["direct_vision"]["requests"], 1)
        self.assertEqual(metricas["direct_vision"]["prompt_tokens"], 500)
        self.assertEqual(metricas["direct_vision"]["cached_tokens"], 100)
        self.assertIsNone(metricas["direct_vision"]["cost_usd"])
        # 1.000 (main) + 500 (visão direta) = 1.500 prompt tokens observados.
        self.assertEqual(metricas["observed_grand_total"]["prompt_tokens"], 1500)
        self.assertEqual(metricas["observed_grand_total"]["requests"], 11)
        self.assertEqual(metricas["derived"]["grand_total_prompt_tokens_per_ready"], 1500.0)
        self.assertEqual(metricas["derived"]["grand_total_requests_per_ready"], 11.0)
        self.assertEqual(metricas["derived"]["direct_vision_requests_per_ready"], 1.0)
        self.assertTrue(metricas["derived"]["grand_total_cost_partial"])


class DecisionLedgerByIDTests(unittest.TestCase):
    """O rótulo da decisão sai do ledger pelo id, nunca do texto do plano."""

    def test_leitura_por_id_ignora_texto_do_plano(self):
        from unicornio_editor.observability import (
            attribution_of,
            read_media_decision_by_id,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 3, decision="auto", score_gap=4,
                                  decision_id="AAA", selected_url="https://cdn/a.jpg")
            record_media_decision(root, 3, decision="choose", score_gap=0,
                                  decision_id="BBB")
            registro = read_media_decision_by_id(root, 3, "AAA")
            self.assertEqual(registro["decision"], "auto")
            self.assertEqual(registro["score_gap"], 4)
            self.assertEqual(registro["selected_url"], "https://cdn/a.jpg")
            self.assertEqual(attribution_of(root, 3, "AAA"), "resolved")
            self.assertEqual(attribution_of(root, 3, "ZZZ"), "invalid")
            self.assertEqual(attribution_of(root, 3, ""), "missing")
            # Id de OUTRO post não é "resolvido" (evita atribuição cruzada).
            self.assertEqual(attribution_of(root, 99, "AAA"), "invalid")

    def test_id_nao_atribuido_de_listicle_e_resolvido(self):
        """Listicle sem --post-id grava post_id=0: o id ainda é do pipeline."""
        from unicornio_editor.observability import attribution_of

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 0, decision="auto", item_index=0,
                                  decision_id="ITEM1")
            self.assertEqual(attribution_of(root, 5, "ITEM1"), "resolved")


if __name__ == "__main__":
    unittest.main()
