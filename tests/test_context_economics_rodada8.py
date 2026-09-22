"""Testes de regressão da oitava rodada (fechamento da instrumentação).

1. `media_plan: []` com histórico no ledger NÃO pode contaminar
   `decision_quality` (nem emitir `decision`, nem entrar no balde do agregador);
2. `decision_scope` do media-validate usa RÓTULOS resolvidos no ledger (não ids):
   `auto + auto` = uniform; e não se declara scope com item missing/invalid;
3. `requests_per_ready` tem o MESMO significado nos dois caminhos (main-only).
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


def _config():
    from unicornio_editor.config import Config

    return Config(
        content_source="wordpress",
        wordpress_url="http://wp.test",
        wordpress_api_base="/wp-json/wp/v2",
        dry_run=True,
    )


class PlanoVazioNaoContaminaTests(unittest.TestCase):
    """A decisão de uma busca anterior não pode entrar em decision_quality."""

    def test_plano_vazio_nao_emite_decision(self):
        from unicornio_editor.workflow import _decision_fields

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 12, decision="auto", score_gap=4)
            campos = _decision_fields(root, 12, [])
        self.assertEqual(campos["decision_attribution"], "missing")
        self.assertNotIn("decision", campos)
        self.assertNotIn("score_gap", campos)
        self.assertEqual(campos["decision_unattributed"], "auto")
        self.assertEqual(campos["score_gap_unattributed"], 4)

    def test_evento_com_atribuicao_missing_nao_entra_no_balde(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                # Evento "sujo": rótulo auto mas atribuição missing (defesa em
                # profundidade — o agregador também recusa).
                append_telemetry(root, "apply_ready", post_id=12, decision="auto",
                                 decision_attribution="missing", first_pass=True)
                append_telemetry(root, "apply_blocked", post_id=12, decision="auto",
                                 decision_attribution="invalid", first_pass=True,
                                 failure_reasons=["imagens_no_corpo"])
                # Evento legítimo, com atribuição resolvida.
                append_telemetry(root, "apply_ready", post_id=13, decision="choose",
                                 decision_attribution="resolved", first_pass=True)
                resumo = read_telemetry_summary(root)
        qualidade = resumo["decision_quality"]
        self.assertNotIn("auto", qualidade)
        self.assertEqual(qualidade["choose"]["apply_ready"], 1)
        # Os contadores GLOBAIS continuam contando os posts (não são por decisão).
        self.assertEqual(resumo["production"]["unique_ready_posts"], 2)
        # 3 primeiras tentativas: 2 READY + 1 BLOCKED (o bloqueio também é uma
        # primeira tentativa — é isso que faz a taxa de sucesso ser honesta).
        self.assertEqual(resumo["production"]["first_pass_attempts"], 3)

    def test_plano_rastreado_entra_no_balde_normalmente(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision_id = record_media_decision(root, 14, decision="auto", score_gap=3)
            from unicornio_editor.workflow import _decision_fields

            campos = _decision_fields(root, 14, [{"decision_id": decision_id}])
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "apply_ready", post_id=14, first_pass=True, **campos)
                resumo = read_telemetry_summary(root)
        self.assertEqual(resumo["decision_quality"]["auto"]["apply_ready"], 1)
        self.assertEqual(resumo["decision_quality"]["auto"]["first_pass_success_rate"], 1.0)


class DecisionScopePorRotuloTests(unittest.TestCase):
    """`auto + auto` é plano UNIFORME (o scope usa rótulos, não ids)."""

    def _payload(self, ids: list[str]) -> dict:
        itens = []
        for indice, identificador in enumerate(ids):
            itens.append({
                "paragraph_index": indice,
                "source_page_url": f"https://pagina/{indice}",
                "direct_image_url": f"https://cdn/{indice}.jpg",
                "author": "a", "license": "l", "license_url": f"https://lic/{indice}",
                "captured_at": "2026-01-01", "credit_text": "c", "alt_text": "alt",
                "is_featured": False, "decision_id": identificador,
            })
        return {
            "site_relevance": {"verdict": "relevant", "reason": "ok"},
            "seo": {"title": "T", "meta_description": "d", "slug": "s", "focus_keyword": "k"},
            "media_plan": itens,
            "cleaned_html": "<p>" + ("palavra " * 400) + "</p>",
        }

    def _rodar(self, root: Path, ids: list[str]) -> list[dict]:
        from unicornio_editor import cli

        arquivo = root / "editorial.json"
        arquivo.write_text(json.dumps(self._payload(ids)), encoding="utf-8")
        client = mock.Mock()
        client.get_post.return_value = {"id": 5, "title": {"raw": "T"}, "featured_media": 0}
        with mock.patch.object(cli, "load_config", return_value=_config()), \
                mock.patch.object(cli, "WordPressClient", return_value=client), \
                mock.patch.object(cli, "validate_media_plan",
                                  return_value={"valid": True, "rejected": [],
                                                "featured_vision": [{"status": "passed"}]}), \
                mock.patch.dict(os.environ, CRON_ENV, clear=False):
            with mock.patch("sys.stdout", new_callable=lambda: open(os.devnull, "w")):
                cli.main(["media-validate", str(arquivo), "--post-id", "5",
                          "--root", str(root)])
        linhas = (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
        eventos = [json.loads(linha) for linha in linhas]
        return [e for e in eventos if e.get("event") == "media_validate_result"
                and "item_index" not in e]

    def test_dois_ids_mesma_decisao_e_uniforme(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 5, decision="auto", decision_id="AAA")
            record_media_decision(root, 5, decision="auto", decision_id="BBB")
            agregado = self._rodar(root, ["AAA", "BBB"])
        self.assertEqual(len(agregado), 1)
        self.assertEqual(agregado[0]["decision_scope"], "uniform")
        self.assertEqual(agregado[0]["attribution"], "resolved")
        self.assertEqual(agregado[0]["decision_ids"], ["AAA", "BBB"])

    def test_dois_ids_decisoes_diferentes_e_mixed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 5, decision="auto", decision_id="AAA")
            record_media_decision(root, 5, decision="choose", decision_id="BBB")
            agregado = self._rodar(root, ["AAA", "BBB"])
        self.assertEqual(agregado[0]["decision_scope"], "mixed")

    def test_com_item_invalido_nao_declara_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 5, decision="auto", decision_id="AAA")
            agregado = self._rodar(root, ["AAA", "ZZZ"])  # ZZZ não existe
        self.assertEqual(agregado[0]["attribution"], "invalid")
        self.assertEqual(agregado[0]["decision_scope"], "")


class RequestsPerReadySymmetryTests(unittest.TestCase):
    """`requests_per_ready` é main-only nos dois caminhos de atribuição."""

    def _banco(self, root: Path) -> Path:
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
            "('cron_9e39343dc6f5_20260922_090524','vision','compression',2,500,10,0,0,0,0.01)"
        )
        db.commit()
        db.close()
        return banco

    def test_mesmo_banco_mesmo_requests_per_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            banco = self._banco(root)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "apply_ready", post_id=1, first_pass=True)
                com_join = session_metrics(root, state_db=banco,
                                           job_id="9e39343dc6f5",
                                           project_root=str(root), hours=24)
            with tempfile.TemporaryDirectory() as vazio:
                # Sessão DIFERENTE da que está no banco: o join não acha nada e o
                # código cai no caminho `window_job` (é o caso real de sessão nova).
                outro = {"UNICORNIO_RUN_SOURCE": "cron",
                         "HERMES_SESSION_ID": "cron_9e39343dc6f5_20260922_235959"}
                with mock.patch.dict(os.environ, outro, clear=False):
                    append_telemetry(Path(vazio), "apply_ready", post_id=1, first_pass=True)
                sem_join = session_metrics(Path(vazio), state_db=banco,
                                           job_id="9e39343dc6f5",
                                           project_root=str(root), hours=24)
        self.assertEqual(com_join["attribution"], "join_sessions")
        self.assertEqual(sem_join["attribution"], "window_job")
        # main-only: 10 requests em ambos.
        self.assertEqual(com_join["derived"]["requests_per_ready"], 10.0)
        self.assertEqual(sem_join["derived"]["requests_per_ready"], 10.0)
        # O total (main + aux) é o mesmo também.
        self.assertEqual(com_join["derived"]["grand_total_requests_per_ready"], 12.0)
        self.assertEqual(sem_join["derived"]["grand_total_requests_per_ready"], 12.0)
        self.assertEqual(com_join["hermes_sessions"]["requests_total"], 12)
        self.assertEqual(sem_join["hermes_sessions"]["requests_total"], 12)


class JoinParcialCaiNoFallbackTests(unittest.TestCase):
    """Join com correspondência PARCIAL não pode ser apresentado como exato."""

    def test_uma_de_duas_sessoes_no_banco_nao_vira_join(self):
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
            agora = int(time.time())
            # Só a sessão A existe no banco; B (mais recente) ainda não consolidou.
            db.execute(
                "INSERT INTO sessions VALUES ('cron_9e39343dc6f5_20260922_090524','cron',?,"
                "10,10000,100,0,0,0,0.05,10)",
                (agora,),
            )
            db.commit()
            db.close()
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "apply_ready", post_id=1, first_pass=True)
            with mock.patch.dict(
                os.environ,
                {"UNICORNIO_RUN_SOURCE": "cron",
                 "HERMES_SESSION_ID": "cron_9e39343dc6f5_20260922_235959"},
                clear=False,
            ):
                append_telemetry(root, "apply_ready", post_id=2, first_pass=True)
            metricas = session_metrics(
                root, state_db=banco, job_id="9e39343dc6f5",
                project_root=str(root), hours=24,
            )
        # 2 sessões na telemetria, 1 no banco => NUNCA join_sessions.
        self.assertEqual(metricas["attribution"], "window_job")
        self.assertNotEqual(metricas["attribution"], "join_sessions")
        # O numerador do fallback é rotulado (window_job), nunca apresentado como
        # join exato das 2 sessões.
        self.assertIn("prefixo do id da sessao", str(metricas["hermes_sessions"]["scope"]))

    def test_join_completo_de_duas_sessoes_vira_join(self):
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
            agora = int(time.time())
            for session_id, tokens in (
                ("cron_9e39343dc6f5_20260922_090524", 10_000),
                ("cron_9e39343dc6f5_20260922_235959", 80_000),
            ):
                db.execute(
                    "INSERT INTO sessions VALUES (?,'cron',?,10,?,100,0,0,0,0.05,10)",
                    (session_id, agora, tokens),
                )
            db.commit()
            db.close()
            for indice, session_id in enumerate(
                ("cron_9e39343dc6f5_20260922_090524",
                 "cron_9e39343dc6f5_20260922_235959"),
                start=1,
            ):
                with mock.patch.dict(
                    os.environ,
                    {"UNICORNIO_RUN_SOURCE": "cron", "HERMES_SESSION_ID": session_id},
                    clear=False,
                ):
                    # Posts DIFERENTES: o READY é contado por post.
                    append_telemetry(root, "apply_ready", post_id=indice, first_pass=True)
            metricas = session_metrics(
                root, state_db=banco, job_id="9e39343dc6f5",
                project_root=str(root), hours=24,
            )
        self.assertEqual(metricas["attribution"], "join_sessions")
        # 90.000 tokens de prompt / 2 READY = 45.000.
        self.assertEqual(metricas["derived"]["prompt_tokens_per_ready"], 45_000.0)
        self.assertIn("2 de 2", metricas["hermes_sessions"]["scope"])


if __name__ == "__main__":
    unittest.main()
