"""Testes de regressão da quarta rodada da auditoria de contexto.

Cobrem os itens novos:

* KPI alinhado: numerador (state.db) e denominador (READY) das MESMAS sessões
  (join por `root_session_id`), com fallback explícito marcado;
* separação main-loop x uso AUXILIAR do Hermes (`session_model_usage`) e o
  `grand_total` (o teto em USD também passou a somar os dois);
* `vision_api_request`: uma requisição HTTP = um evento, com o `usage` real e a
  contagem de low/high (a escalada agora acontece de verdade);
* ledger de mídia append-only (duas buscas do mesmo post preservam as DUAS
  decisões) e por ITEM (listicle);
* `run_sources.ready` contando de verdade;
* guard de bytes de contexto filtrado por cron/job.
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
    read_media_decision,
    read_media_decisions,
    read_telemetry_summary,
    record_media_decision,
)
from unicornio_editor.session_metrics import session_metrics

CRON_ENV = {"UNICORNIO_RUN_SOURCE": "cron",
            "HERMES_SESSION_ID": "cron_9e39343dc6f5_20260922_090524"}


class SessionJoinTests(unittest.TestCase):
    """O KPI precisa cruzar telemetria e state.db pelas MESMAS sessões."""

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
        agora = int(time.time())
        # Sessão do cron observada na telemetria (1 READY) ...
        db.execute(
            "INSERT INTO sessions VALUES ('cron_9e39343dc6f5_20260922_090524','cron',?,"
            "40,1000,200,9000,0,100,0.20,40)",
            (agora,),
        )
        # ... e uma sessão ANTIGA na mesma janela de 24h, que NÃO pode entrar no
        # numerador do KPI (era ela que inflava tokens/READY).
        db.execute(
            "INSERT INTO sessions VALUES ('cron_9e39343dc6f5_20260922_070447','cron',?,"
            "500,999999,999999,99999999,0,0,9.99,500)",
            (agora - 3600,),
        )
        # Uso auxiliar (vision) da sessão observada.
        db.execute(
            "INSERT INTO session_model_usage VALUES "
            "('cron_9e39343dc6f5_20260922_090524','gpt-4o-mini','vision',3,5000,300,0,0,0,0.05)"
        )
        db.commit()
        db.close()
        return banco

    def test_numerador_usa_apenas_as_sessoes_observadas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            banco = self._banco(root)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "cmd_output", command="cards", bytes=2048, post_id=1)
                append_telemetry(root, "apply_ready", post_id=1, first_pass=True)
                metricas = session_metrics(
                    root, state_db=banco, job_id="9e39343dc6f5",
                    project_root=str(root), hours=24,
                )
        self.assertEqual(metricas["attribution"], "join_sessions")
        # Só a sessão observada: 1000 + 9000 = 10.000 prompt tokens (a sessão
        # antiga de 100M NÃO entra), 1 READY => 10.000 tokens/READY.
        self.assertEqual(metricas["hermes_sessions"]["prompt_tokens"], 10000)
        self.assertEqual(metricas["derived"]["prompt_tokens_per_ready"], 10000.0)
        self.assertEqual(metricas["derived"]["tool_context_bytes_per_ready"], 2048.0)
        # Auxiliar (vision) separado do main-loop.
        self.assertEqual(metricas["hermes_sessions"]["aux"]["input_tokens"], 5000)
        self.assertEqual(metricas["hermes_sessions"]["aux"]["tasks"], {"vision": 3})
        self.assertEqual(metricas["derived"]["aux_prompt_tokens_per_ready"], 5000.0)
        self.assertEqual(metricas["derived"]["aux_cost_per_ready_usd"], 0.05)
        # Grand total = main + aux, no custo também.
        self.assertEqual(metricas["hermes_sessions"]["grand_total"]["cost_usd"], 0.25)
        self.assertEqual(metricas["derived"]["grand_total_cost_per_ready_usd"], 0.25)
        self.assertEqual(metricas["derived"]["cost_per_ready_usd"], 0.25)

    def test_sem_evento_instrumentado_marca_o_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            banco = self._banco(root)
            # Nenhum evento => sem sessões observadas: cai na janela + job, e o
            # resultado fica MARCADO como tal (não se vende join).
            metricas = session_metrics(
                root, state_db=banco, job_id="9e39343dc6f5", project_root=str(root), hours=24
            )
        self.assertEqual(metricas["attribution"], "window_job")
        self.assertIsNotNone(metricas["hermes_sessions"])


class VisionRequestTelemetryTests(unittest.TestCase):
    """Uma requisição HTTP = um evento, com usage; low e high contam."""

    def test_escalada_conta_duas_requisicoes(self):
        from unicornio_editor.media import vision_gate
        from unicornio_editor.media.vision_gate import verify_image_subject

        respostas = [
            {"status": "AMBIGUOUS", "confidence": 0.5, "visual_type": "other"},
            {"status": "MATCH", "confidence": 0.9, "visual_type": "key_art"},
        ]
        uso = {"prompt_tokens": 111, "completion_tokens": 7,
               "prompt_tokens_details": {"cached_tokens": 50}}

        def fake_urlopen(request, timeout=None):
            class _Resp:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *_a):
                    return False

                def read(self_inner):
                    corpo = {
                        "choices": [{"message": {"content": json.dumps(respostas.pop(0))}}],
                        "usage": uso,
                    }
                    return json.dumps(corpo).encode()

            return _Resp()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False), mock.patch.object(
                vision_gate, "urlopen", side_effect=fake_urlopen
            ):
                ok, razao = verify_image_subject(
                    image_url="https://media/x.jpg", subject="Redfall",
                    api_key="k", base_url="https://api.vision/v1", model="vision-test",
                    detail="low", allow_high=True, root=root,
                )
            self.assertTrue(ok, razao)
            resumo = read_telemetry_summary(root)
        economia = resumo["media_economy"]
        self.assertEqual(economia["vision_api_requests"], 2)  # low + high
        self.assertEqual(economia["vision_low_requests"], 1)
        self.assertEqual(economia["vision_high_requests"], 1)
        self.assertEqual(economia["vision_input_tokens"], 222)
        self.assertEqual(economia["vision_cached_tokens"], 100)
        self.assertEqual(economia["vision_output_tokens"], 14)
        self.assertEqual(economia["vision_errors"], 0)


class MediaDecisionLedgerTests(unittest.TestCase):
    """Ledger append-only: cada imagem/item tem sua decisão."""

    def test_duas_buscas_do_mesmo_post_preservam_as_duas_decisoes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            id1 = record_media_decision(
                root, 114245, decision="auto", score_gap=4, query="img1",
                selected_url="https://cdn/1.jpg", coverage="web",
            )
            id2 = record_media_decision(
                root, 114245, decision="choose", score_gap=0, query="img2", coverage="mixed",
            )
            self.assertNotEqual(id1, id2)
            registros = read_media_decisions(root, 114245)
            self.assertEqual([r["decision"] for r in registros], ["auto", "choose"])
            self.assertEqual([r["score_gap"] for r in registros], [4, 0])
            # `read_media_decision` devolve a ÚLTIMA (é o que os gates usam).
            self.assertEqual(read_media_decision(root, 114245)["decision"], "choose")
            self.assertEqual(read_media_decision(root, 114245)["decision_id"], id2)

    def test_item_de_listicle_tem_decisao_propria(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for indice, decisao in enumerate(("auto", "choose", "none")):
                record_media_decision(
                    root, 900, decision=decisao, query=f"item{indice}",
                    subject=f"Obra {indice}", item_index=indice,
                )
            registros = read_media_decisions(root, 900)
        self.assertEqual([r["item_index"] for r in registros], [0, 1, 2])
        self.assertEqual([r["decision"] for r in registros], ["auto", "choose", "none"])
        self.assertTrue(all(r["decision_id"] for r in registros))

    def test_ledger_antigo_json_continua_legivel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work").mkdir(parents=True, exist_ok=True)
            (root / "work" / "media_decisions.json").write_text(
                json.dumps({"777": {"decision": "reuse", "score_gap": None}}),
                encoding="utf-8",
            )
            self.assertEqual(read_media_decision(root, 777)["decision"], "reuse")


class RunSourceReadyTests(unittest.TestCase):
    def test_run_sources_conta_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "apply_ready", post_id=1)
                append_telemetry(root, "apply_ready", post_id=2)
            with mock.patch.dict(
                os.environ,
                {"UNICORNIO_RUN_SOURCE": "manual",
                 "HERMES_SESSION_ID": "20260922_094321_b7db55"},
                clear=False,
            ):
                append_telemetry(root, "cmd_output", command="cards", bytes=100, post_id=3)
            resumo = read_telemetry_summary(root)
        self.assertEqual(resumo["run_sources"]["cron"]["ready"], 2)
        self.assertEqual(resumo["run_sources"]["manual"]["ready"], 0)
        self.assertEqual(resumo["run_sources"]["cron"]["events"], 2)


class ContextBytesGuardFilterTests(unittest.TestCase):
    """O teto de bytes só considera o cron do job (não execução manual)."""

    def _guard(self):
        import importlib.util

        caminho = Path(__file__).resolve().parents[1] / "hermes" / "cost_guard.py"
        spec = importlib.util.spec_from_file_location("cost_guard_r4", caminho)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        return modulo

    def test_bytes_manuais_nao_bloqueiam_o_cron(self):
        modulo = self._guard()
        with tempfile.TemporaryDirectory() as directory:
            caminho = Path(directory) / "telemetry.jsonl"
            linhas = [
                {"event": "cmd_output", "bytes": 10, "command": "cards",
                 "run_source": "cron", "cron_job_id": "editorial",
                 "ts": "2026-09-22T10:00:00+00:00"},
                {"event": "cmd_output", "bytes": 500_000, "command": "cards",
                 "run_source": "manual", "cron_job_id": "",
                 "ts": "2026-09-22T10:00:01+00:00"},
                {"event": "cmd_output", "bytes": 20, "command": "content",
                 "run_source": "cron", "cron_job_id": "editorial",
                 "ts": "2026-09-22T10:00:02+00:00"},
                # Evento antigo, sem origem: não entra no teto.
                {"event": "cmd_output", "bytes": 90_000, "command": "cards",
                 "ts": "2026-09-22T10:00:03+00:00"},
            ]
            caminho.write_text(
                "\n".join(json.dumps(linha) for linha in linhas), encoding="utf-8"
            )
            medicao = modulo.context_bytes_in_last_24h(
                caminho, hours=24, job_id="editorial", run_source="cron"
            )
        self.assertEqual(medicao, (30, 2))


class SensitiveFieldFilterTests(unittest.TestCase):
    """Contadores com nome de credencial NÃO podem ser descartados."""

    def test_contadores_de_tokens_entram_e_credenciais_nao(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(
                root, "evento_teste",
                input_tokens=10, cached_tokens=3, output_tokens=2,
                api_key="valor-de-credencial", authorization="Bearer xyz",
                password=1234,  # número não é segredo em claro
            )
            linha = json.loads(
                (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8").strip()
            )
        self.assertEqual(linha["input_tokens"], 10)
        self.assertEqual(linha["cached_tokens"], 3)
        self.assertEqual(linha["output_tokens"], 2)
        self.assertNotIn("api_key", linha)
        self.assertNotIn("authorization", linha)
        # Senha numérica não é credencial em claro (o filtro protege TEXTO).
        self.assertEqual(linha["password"], 1234)


if __name__ == "__main__":
    unittest.main()
