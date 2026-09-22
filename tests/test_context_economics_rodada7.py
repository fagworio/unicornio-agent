"""Testes de regressão da sétima rodada (instrumentação, último passo).

1. `join_sessions` NÃO pode perder os requests auxiliares (senão o primeiro ciclo
   medido parece mais barato só por trocar o método de atribuição);
2. `decision_attribution_rate` mede ITENS (o evento agregado do post não entra);
   `mixed` é outra dimensão (`mixed_plan_count` / `decision_scope`);
3. `_decision_fields()` só rotula plano TOTALMENTE rastreado (todos os itens com
   id, todos resolvidos) e com decisão única;
4. tokens de visão marcados como parciais quando houve requisição sem `usage`.
"""

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

from unicornio_editor.observability import (
    append_telemetry,
    read_telemetry_summary,
    record_media_decision,
)
from unicornio_editor.session_metrics import session_metrics

CRON_ENV = {"UNICORNIO_RUN_SOURCE": "cron",
            "HERMES_SESSION_ID": "cron_9e39343dc6f5_20260922_090524"}


def _banco(root: Path, *, aux_chamadas: int = 2) -> Path:
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
    db.execute(
        "INSERT INTO session_model_usage VALUES "
        "('cron_9e39343dc6f5_20260922_090524','vision','compression',?,500,10,0,0,0,0.01)",
        (aux_chamadas,),
    )
    db.commit()
    db.close()
    return banco


class JoinSessionsAuxRequestsTests(unittest.TestCase):
    """O caminho preferencial (join) tem de contar os requests auxiliares."""

    def test_join_sessions_soma_main_aux_e_visao_direta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            banco = _banco(root, aux_chamadas=2)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "apply_ready", post_id=1, first_pass=True)
                append_telemetry(root, "vision_api_request", detail="low",
                                 input_tokens=100, cached_tokens=0, output_tokens=5)
                metricas = session_metrics(
                    root, state_db=banco, job_id="9e39343dc6f5",
                    project_root=str(root), hours=24,
                )
        # O join tem de estar ativo (há sessão na telemetria).
        self.assertEqual(metricas["attribution"], "join_sessions")
        hermes = metricas["hermes_sessions"]
        self.assertEqual(hermes["main_requests"], 10)
        self.assertEqual(hermes["aux_requests"], 2)
        self.assertEqual(hermes["grand_total"]["requests"], 12)
        self.assertEqual(hermes["requests"], 12)
        self.assertEqual(metricas["direct_vision"]["requests"], 1)
        # 10 (main) + 2 (aux) + 1 (visão direta) = 13.
        self.assertEqual(metricas["observed_grand_total"]["requests"], 13)
        self.assertEqual(metricas["derived"]["grand_total_requests_per_ready"], 13.0)

    def test_janela_job_e_join_concordam_nos_auxiliares(self):
        """Os dois métodos de atribuição não podem divergir nos auxiliares."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            banco = _banco(root, aux_chamadas=7)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "apply_ready", post_id=1, first_pass=True)
                com_join = session_metrics(
                    root, state_db=banco, job_id="9e39343dc6f5",
                    project_root=str(root), hours=24,
                )
            # Mesmo banco, SEM sessão na telemetria: cai no window_job.
            with tempfile.TemporaryDirectory() as vazio:
                sem_telemetria = session_metrics(
                    Path(vazio), state_db=banco, job_id="9e39343dc6f5",
                    project_root=str(root), hours=24,
                )
        self.assertEqual(com_join["attribution"], "join_sessions")
        self.assertEqual(sem_telemetria["attribution"], "window_job")
        self.assertEqual(
            com_join["hermes_sessions"]["aux_requests"],
            sem_telemetria["hermes_sessions"]["aux_requests"],
        )
        self.assertEqual(com_join["hermes_sessions"]["aux_requests"], 7)


class AttribuitionRatePerItemTests(unittest.TestCase):
    """A taxa conta ITENS; plano misto é outra dimensão."""

    def _itens(self, total: int, com_id: str) -> list[dict]:
        itens = []
        for indice in range(total):
            item = {
                "post_id": 4, "item_index": indice, "valid": True,
                "rejected_items": 0, "attribution": "resolved",
                "decision": "auto", "decision_id": com_id,
            }
            itens.append(item)
        return itens

    def test_dez_itens_atribuidos_com_plano_misto_dao_100_por_cento(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                for indice in range(10):
                    append_telemetry(root, "media_validate_result", post_id=4,
                                     item_index=indice, valid=True, rejected_items=0,
                                     attribution="resolved", decision="auto",
                                     decision_id="AAA")
                # Agregado do post (não é item): plano misto.
                append_telemetry(root, "media_validate_result", post_id=4,
                                 valid=False, rejected_items=0,
                                 attribution="resolved", decision="",
                                 decision_scope="mixed")
                resumo = read_telemetry_summary(root)
        atrib = resumo["decision_attribution"]
        self.assertEqual(atrib["resolved"], 10)
        self.assertEqual(atrib["itens"], 10)
        # 100% dos ITENS atribuídos (antes: 10/11 = 90,9% por causa do agregado).
        self.assertEqual(atrib["decision_attribution_rate"], 1.0)
        self.assertEqual(atrib["mixed_plan_count"], 1)

    def test_agregado_nao_entra_na_taxa(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "media_validate_result", post_id=4,
                                 item_index=0, valid=True, rejected_items=0,
                                 attribution="resolved", decision="auto",
                                 decision_id="AAA")
                append_telemetry(root, "media_validate_result", post_id=4,
                                 item_index=1, valid=False, rejected_items=1,
                                 attribution="invalid", decision="", decision_id="ZZZ")
                append_telemetry(root, "media_validate_result", post_id=4,
                                 valid=False, rejected_items=1,
                                 attribution="invalid", decision="", decision_id="")
                resumo = read_telemetry_summary(root)
        atrib = resumo["decision_attribution"]
        self.assertEqual(atrib["itens"], 2)
        self.assertEqual(atrib["resolved"], 1)
        self.assertEqual(atrib["invalid"], 1)
        self.assertEqual(atrib["decision_attribution_rate"], 0.5)


class StrictAttributionInApplyTests(unittest.TestCase):
    """O apply só rotula plano TOTALMENTE rastreado com decisão única."""

    def _campos(self, root: Path, post_id: int, plan: list[dict]) -> dict:
        from unicornio_editor.workflow import _decision_fields

        return _decision_fields(root, post_id, plan)

    def test_plano_parcialmente_rastreado_nao_rotula(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 7, decision="auto", decision_id="AAA")
            campos = self._campos(root, 7, [
                {"decision_id": "AAA"},
                {"source_page_url": "https://pagina/2"},  # imagem SEM rastro
            ])
        self.assertEqual(campos["decision_attribution"], "missing")
        self.assertNotIn("decision", campos)

    def test_plano_com_id_invalido_nao_rotula_mesmo_com_valido(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 7, decision="auto", decision_id="AAA")
            campos = self._campos(root, 7, [
                {"decision_id": "AAA"},
                {"decision_id": "BBB"},  # não existe no ledger
            ])
        self.assertEqual(campos["decision_attribution"], "invalid")
        self.assertNotIn("decision", campos)
        self.assertEqual(campos["decision_ids"], ["AAA", "BBB"])

    def test_plano_totalmente_rastreado_e_uniforme_rotula(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 7, decision="auto", decision_id="AAA")
            record_media_decision(root, 7, decision="auto", decision_id="BBB")
            campos = self._campos(root, 7, [
                {"decision_id": "AAA"}, {"decision_id": "BBB"},
            ])
        self.assertEqual(campos["decision_attribution"], "resolved")
        self.assertEqual(campos["decision_scope"], "uniform")
        self.assertEqual(campos["decision"], "auto")

    def test_plano_totalmente_rastreado_mas_misto_nao_rotula(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 7, decision="auto", decision_id="AAA")
            record_media_decision(root, 7, decision="choose", decision_id="BBB")
            campos = self._campos(root, 7, [
                {"decision_id": "AAA"}, {"decision_id": "BBB"},
            ])
        # Atribuição RESOLVIDA (todos os itens rastreados), mas sem rótulo único.
        self.assertEqual(campos["decision_attribution"], "resolved")
        self.assertEqual(campos["decision_scope"], "mixed")
        self.assertNotIn("decision", campos)


class VisionPartialTokensTests(unittest.TestCase):
    """Requisição sem `usage` => total de tokens é lower bound (sinalizado)."""

    def test_erro_sem_usage_marca_tokens_como_parciais(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "apply_ready", post_id=1, first_pass=True)
                append_telemetry(root, "vision_api_request", detail="low",
                                 input_tokens=200, cached_tokens=50, output_tokens=5)
                append_telemetry(root, "vision_api_request", detail="high",
                                 error="HTTP 500")  # sem usage
                metricas = session_metrics(
                    root, state_db=root / "inexistente.db",
                    job_id="9e39343dc6f5", project_root=str(root), hours=24,
                )
        visao = metricas["direct_vision"]
        self.assertEqual(visao["requests"], 2)
        self.assertEqual(visao["errors"], 1)
        self.assertEqual(visao["requests_without_usage"], 1)
        self.assertTrue(visao["tokens_partial"])
        self.assertEqual(visao["prompt_tokens"], 200)  # lower bound
        self.assertTrue(metricas["observed_grand_total"]["tokens_partial"])

    def test_sem_erro_nao_marca_parcial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "apply_ready", post_id=1, first_pass=True)
                append_telemetry(root, "vision_api_request", detail="low",
                                 input_tokens=200, cached_tokens=50, output_tokens=5)
                metricas = session_metrics(
                    root, state_db=root / "inexistente.db",
                    job_id="9e39343dc6f5", project_root=str(root), hours=24,
                )
        visao = metricas["direct_vision"]
        self.assertFalse(visao["tokens_partial"])
        self.assertEqual(visao["requests_without_usage"], 0)
        self.assertFalse(metricas["observed_grand_total"]["tokens_partial"])


if __name__ == "__main__":
    unittest.main()
