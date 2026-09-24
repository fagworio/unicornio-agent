"""P0 do canary: o teto de USD/volume precisa cobrir as chamadas DIRETAS.

Antes desta rodada o guard media so o accounting do Hermes e declarava
``cost_direct_vision_usd = None``: nem o editorial direto (provider novo,
``editorial_model_request``) nem a visao direta (``vision_api_request``) entravam
no teto de ``HERMES_EDITORIAL_DAILY_COST_LIMIT_USD``.

Cobertura:
1. ``cost_guard`` contabiliza as DUAS familias diretas;
2. relatorio separado em ``cost_main_hermes_usd`` / ``cost_aux_hermes_usd`` /
   ``cost_editorial_direct_usd`` / ``cost_vision_direct_usd`` /
   ``grand_total_cost_usd``;
3. o bloqueio usa ``grand_total_cost_usd`` (nao so o gasto do state.db);
4. os limites de volume usam ``grand_total_requests`` /
   ``grand_total_prompt_tokens`` com o editorial direto incluido;
5. os PRODUTORES gravam ``model_cost_usd`` no evento, com preco configuravel por
   provider/modelo no ambiente (nada de preco escondido no codigo).
"""

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
PRECO_EDITORIAL = ("EDITORIAL_INPUT_COST_PER_1M_USD", "EDITORIAL_OUTPUT_COST_PER_1M_USD")
PRECO_VISAO = ("EDITOR_VISION_INPUT_COST_PER_1M_USD", "EDITOR_VISION_OUTPUT_COST_PER_1M_USD")
JOB = "9e39343dc6f5"
SESSAO = f"cron_{JOB}_20260924_120000"


def _guard():
    """Carrega hermes/cost_guard.py como modulo (nao e um pacote instalado)."""
    caminho = RAIZ / "hermes" / "cost_guard.py"
    spec = importlib.util.spec_from_file_location("cost_guard_diretos", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _ts(minutos_atras: int) -> str:
    from datetime import datetime, timedelta, timezone

    momento = datetime.now(timezone.utc) - timedelta(minutes=minutos_atras)
    return momento.isoformat(timespec="seconds")


def _banco(root: Path, *, custo_main: float, custo_aux: float = 0.0) -> Path:
    """state.db minimo com a MESMA forma que o guard le (sessions + usage)."""
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
    db.execute(
        "INSERT INTO sessions VALUES (?, 'cron', ?, 40, 1000, 200, 9000, 0, 0, ?)",
        (SESSAO, int(time.time()), custo_main),
    )
    db.execute(
        "INSERT INTO session_model_usage VALUES (?, 'vision', 'compression', 2, "
        "1000, 10, 0, 0, 0, ?)",
        (SESSAO, custo_aux),
    )
    db.commit()
    db.close()
    return banco


def _telemetria(root: Path, linhas: list[dict], nome: str = "telemetry.jsonl") -> Path:
    caminho = root / "work" / nome
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(
        "\n".join(json.dumps(linha) for linha in linhas), encoding="utf-8"
    )
    return caminho


class RelatorioDeCustoDiretoTests(unittest.TestCase):
    """Itens 1, 2 e 4: as duas familias diretas entram em custo e volume."""

    def _linhas(self) -> list[dict]:
        return [
            {
                "event": "editorial_model_request", "batch_id": "b1", "batch_size": 2,
                "run_source": "cron", "cron_job_id": JOB,
                "input_tokens": 5778, "output_tokens": 205, "ts": _ts(3),
            },
            {
                "event": "vision_api_request", "detail": "low", "run_source": "cron",
                "cron_job_id": JOB, "input_tokens": 14000, "cached_tokens": 0,
                "output_tokens": 45, "ts": _ts(2),
            },
            # Execucao MANUAL nao entra no teto do cron.
            {
                "event": "editorial_model_request", "run_source": "manual",
                "input_tokens": 999_999, "output_tokens": 999_999, "ts": _ts(1),
            },
            # Evento de outra familia/etapa nao e custo direto.
            {
                "event": "cmd_output", "command": "cards", "bytes": 1234,
                "run_source": "cron", "cron_job_id": JOB, "ts": _ts(1),
            },
        ]

    def test_direto_entra_no_custo_e_no_volume(self):
        modulo = _guard()
        with tempfile.TemporaryDirectory() as diretorio:
            root = Path(diretorio)
            banco = _banco(root, custo_main=0.20, custo_aux=0.01)
            telemetria = _telemetria(root, self._linhas())
            med = modulo.usage_measurement_in_last_24h(
                banco, JOB, str(root), telemetry_path=telemetria,
                editorial_price=(0.15, 0.60), vision_price=(0.15, 0.60),
            )
        # Item 2: cinco fatias nomeadas.
        self.assertEqual(med["cost_main_hermes_usd"], 0.20)
        self.assertEqual(med["cost_aux_hermes_usd"], 0.01)
        self.assertAlmostEqual(med["cost_editorial_direct_usd"], 0.00099, places=6)
        self.assertAlmostEqual(med["cost_vision_direct_usd"], 0.002127, places=6)
        self.assertEqual(
            med["grand_total_cost_usd"],
            round(0.20 + 0.01 + 0.0009897 + 0.002127, 6),
        )
        # O alias historico passa a ser o grand total (era main + auxiliar).
        self.assertEqual(med["cost_usd"], med["grand_total_cost_usd"])
        self.assertEqual(med["cost_main_usd"], med["cost_main_hermes_usd"])
        self.assertFalse(med["cost_partial"])
        self.assertEqual(med["direct_unpriced_requests"], 0)
        # Item 4: volume com o editorial direto incluido (e manual fora).
        self.assertEqual(med["direct_editorial_requests"], 1)
        self.assertEqual(med["direct_editorial_prompt_tokens"], 5778)
        self.assertEqual(med["direct_vision_requests"], 1)
        self.assertEqual(med["grand_total_requests"], 40 + 2 + 1 + 1)
        self.assertEqual(med["grand_total_prompt_tokens"], 10_000 + 1_000 + 14_000 + 5_778)
        self.assertEqual(med["prompt_tokens"], med["grand_total_prompt_tokens"])

    def test_sem_preco_o_custo_direto_fica_indeterminado(self):
        """Sem preco configurado NADA e inventado: o total sai como parcial."""
        modulo = _guard()
        with tempfile.TemporaryDirectory() as diretorio:
            root = Path(diretorio)
            banco = _banco(root, custo_main=0.20)
            telemetria = _telemetria(root, self._linhas())
            med = modulo.usage_measurement_in_last_24h(
                banco, JOB, str(root), telemetry_path=telemetria,
                editorial_price=(0.0, 0.0), vision_price=(0.0, 0.0),
            )
        self.assertIsNone(med["cost_editorial_direct_usd"])
        self.assertIsNone(med["cost_vision_direct_usd"])
        self.assertTrue(med["cost_partial"])
        self.assertEqual(med["direct_unpriced_requests"], 2)
        # Sem numero conhecido, o total e o que se sabe (main + aux) — nunca zero.
        self.assertEqual(med["grand_total_cost_usd"], 0.20)
        # Volume continua contado, mesmo sem preco.
        self.assertEqual(med["grand_total_requests"], 44)

    def test_model_cost_usd_do_evento_prevalece_sobre_o_preco_do_ambiente(self):
        """O evento grava o preco VIGENTE na chamada; o guard prefere ele."""
        modulo = _guard()
        linhas = self._linhas()
        linhas[0]["model_cost_usd"] = 0.005
        with tempfile.TemporaryDirectory() as diretorio:
            root = Path(diretorio)
            banco = _banco(root, custo_main=0.20)
            telemetria = _telemetria(root, linhas)
            med = modulo.usage_measurement_in_last_24h(
                banco, JOB, str(root), telemetry_path=telemetria,
                editorial_price=(0.15, 0.60), vision_price=(0.15, 0.60),
            )
        self.assertEqual(med["cost_editorial_direct_usd"], 0.005)
        self.assertEqual(
            med["grand_total_cost_usd"], round(0.20 + 0.005 + 0.002127, 6)
        )

    def test_preco_do_ambiente_e_lido_quando_o_evento_nao_traz_custo(self):
        modulo = _guard()
        with tempfile.TemporaryDirectory() as diretorio:
            root = Path(diretorio)
            banco = _banco(root, custo_main=0.20)
            telemetria = _telemetria(root, self._linhas())
            ambiente = {PRECO_EDITORIAL[0]: "0.15", PRECO_EDITORIAL[1]: "0.60",
                        PRECO_VISAO[0]: "0.15", PRECO_VISAO[1]: "0.60"}
            with mock.patch.dict(os.environ, ambiente, clear=False):
                med = modulo.usage_measurement_in_last_24h(
                    banco, JOB, str(root), telemetry_path=telemetria
                )
        self.assertAlmostEqual(med["cost_editorial_direct_usd"], 0.00099, places=6)
        self.assertAlmostEqual(med["cost_vision_direct_usd"], 0.002127, places=6)


class TetoUsaOGrandTotalTests(unittest.TestCase):
    """Item 3: o bloqueio olha o grand total, nao so o gasto do state.db."""

    def _rodar_guard(self, *, telemetria: Path, limite: float) -> dict:
        modulo = _guard()
        argv = [
            "cost_guard.py",
            "--state-db", str(self.banco),
            "--job-id", JOB,
            "--project-root", str(self.root),
            "--limit", str(limite),
            "--telemetry", str(telemetria),
            "--price-editorial-in", "0.15", "--price-editorial-out", "0.60",
            "--price-vision-in", "0.15", "--price-vision-out", "0.60",
        ]
        saida = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(saida):
            codigo = modulo.main()
        return {"rc": codigo, **json.loads(saida.getvalue())}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # 0,70 no Hermes: ABAIXO do teto de 0,80 sozinho.
        self.banco = _banco(self.root, custo_main=0.70)
        self.telemetria = _telemetria(self.root, [
            {"event": "editorial_model_request", "run_source": "cron",
             "cron_job_id": JOB, "input_tokens": 5778, "output_tokens": 205,
             "model_cost_usd": 0.12, "ts": _ts(2)},
            {"event": "vision_api_request", "run_source": "cron",
             "cron_job_id": JOB, "input_tokens": 14000, "output_tokens": 45,
             "model_cost_usd": 0.03, "ts": _ts(1)},
        ])

    def tearDown(self):
        self._tmp.cleanup()

    def test_bloqueia_quando_o_direto_estoura_o_teto(self):
        resultado = self._rodar_guard(telemetria=self.telemetria, limite=0.80)
        self.assertEqual(resultado["decision"], "block")
        self.assertEqual(resultado["reason"], "cost_usd")
        self.assertEqual(resultado["rc"], 10)
        # O gasto do Hermes sozinho nao chega ao teto: quem bloqueou foi o direto.
        self.assertEqual(resultado["cost"]["cost_main_hermes_usd"], 0.70)
        self.assertEqual(resultado["cost"]["cost_editorial_direct_usd"], 0.12)
        self.assertEqual(resultado["cost"]["cost_vision_direct_usd"], 0.03)
        self.assertEqual(resultado["cost"]["grand_total_cost_usd"], 0.85)
        self.assertEqual(resultado["measured"]["cost_usd"], 0.85)

    def test_sem_o_direto_o_mesmo_gasto_do_hermes_nao_bloqueia(self):
        """Prova do furo antigo: main 0,70 < 0,80 passava sem enxergar o direto."""
        vazio = self.root / "work" / "inexistente.jsonl"
        resultado = self._rodar_guard(telemetria=vazio, limite=0.80)
        self.assertEqual(resultado["decision"], "allow")
        self.assertEqual(resultado["cost"]["grand_total_cost_usd"], 0.70)

    def test_limite_de_requests_inclui_o_editorial_direto(self):
        # main+aux+visao = 43; com o editorial = 44. Um teto de 44 bloqueia.
        bloqueio = self._rodar_guard_volume("--limit-requests", 44)
        self.assertEqual(bloqueio["decision"], "block")
        self.assertEqual(bloqueio["reason"], "requests")
        self.assertEqual(bloqueio["requests"]["direct_editorial"], 1)
        self.assertEqual(bloqueio["requests"]["grand_total"], 44)
        # Sem o editorial direto o total seria 43: o limite nao bloquearia.
        folga = self._rodar_guard_volume("--limit-requests", 45)
        self.assertEqual(folga["decision"], "allow")

    def test_limite_de_prompt_tokens_inclui_o_editorial_direto(self):
        total = 10_000 + 1_000 + 14_000 + 5_778
        bloqueio = self._rodar_guard_volume("--limit-prompt-tokens", total)
        self.assertEqual(bloqueio["decision"], "block")
        self.assertEqual(bloqueio["reason"], "prompt_tokens")
        self.assertEqual(bloqueio["tokens"]["direct_editorial_prompt"], 5_778)
        self.assertEqual(bloqueio["tokens"]["grand_total_prompt"], total)
        folga = self._rodar_guard_volume("--limit-prompt-tokens", total + 1)
        self.assertEqual(folga["decision"], "allow")

    def _rodar_guard_volume(self, flag: str, valor: float) -> dict:
        modulo = _guard()
        argv = [
            "cost_guard.py",
            "--state-db", str(self.banco),
            "--job-id", JOB,
            "--project-root", str(self.root),
            flag, str(valor),
            "--telemetry", str(self.telemetria),
            "--price-editorial-in", "0.15", "--price-editorial-out", "0.60",
            "--price-vision-in", "0.15", "--price-vision-out", "0.60",
        ]
        saida = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(saida):
            modulo.main()
        return json.loads(saida.getvalue())


class ProdutoresGravamCustoDoEventoTests(unittest.TestCase):
    """Item 5: quem faz a chamada direta grava `model_cost_usd` no evento."""

    def test_vision_direta_registra_custo_configuravel(self):
        from unicornio_editor.media.vision_gate import _registrar_chamada

        ambiente = {PRECO_VISAO[0]: "0.15", PRECO_VISAO[1]: "0.60",
                    "UNICORNIO_RUN_SOURCE": "cron"}
        with tempfile.TemporaryDirectory() as diretorio:
            root = Path(diretorio)
            with mock.patch.dict(os.environ, ambiente, clear=False):
                _registrar_chamada(
                    root, detail="low", model="gpt-4o-mini",
                    base_url="http://127.0.0.1:1/v1",
                    usage={"prompt_tokens": 1000, "completion_tokens": 20},
                )
            evento = json.loads(
                (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8").strip()
            )
        self.assertEqual(evento["event"], "vision_api_request")
        # (1000 * 0,15 + 20 * 0,60) / 1M = 0,000162
        self.assertAlmostEqual(evento["model_cost_usd"], 0.000162, places=9)
        # O guard le esse numero sem depender de preco no ambiente dele.
        modulo = _guard()
        with tempfile.TemporaryDirectory() as diretorio:
            root = Path(diretorio)
            telemetria = _telemetria(root, [evento])
            dados = modulo.vision_direct_usage(telemetria, run_source="cron")
        self.assertEqual(dados["requests"], 1)
        self.assertAlmostEqual(dados["cost_usd"], 0.000162, places=9)

    def test_sem_preco_configurado_o_evento_nao_inventa_custo(self):
        from unicornio_editor.media.vision_gate import _registrar_chamada

        ambiente = {PRECO_VISAO[0]: "", PRECO_VISAO[1]: "",
                    "UNICORNIO_RUN_SOURCE": "cron"}
        with tempfile.TemporaryDirectory() as diretorio:
            root = Path(diretorio)
            with mock.patch.dict(os.environ, ambiente, clear=False):
                _registrar_chamada(
                    root, detail="low", model="gpt-4o-mini",
                    base_url="http://127.0.0.1:1/v1",
                    usage={"prompt_tokens": 1000, "completion_tokens": 20},
                )
            evento = json.loads(
                (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8").strip()
            )
        self.assertNotIn("model_cost_usd", evento)


_EDITORIAL = {
    "site_relevance": {
        "decision": "skip", "confidence": 0.98,
        "reason": "fora do escopo", "matched_topics": [],
    },
    "media_plan": [],
    "needs_trailer": False,
    "trailer_url": None,
    "game_name": None,
}


class _EditorialHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        corpo = json.dumps({
            "choices": [{"message": {"content": json.dumps({
                "batch_id": "editorial-test",
                "results": [{"post_id": 1, "status": "ok", "reason": "",
                             "editorial": dict(_EDITORIAL)}],
            })}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *_args):
        pass


class EditorialProviderRegistraCustoTests(unittest.TestCase):
    """O provider editorial novo tambem entra no custo direto."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _EditorialHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_evento_do_editorial_grava_custo_e_entra_no_guard(self):
        from unicornio_editor.editorial_provider import generate_editorial_batch

        ambiente = {PRECO_EDITORIAL[0]: "0.15", PRECO_EDITORIAL[1]: "0.60",
                    "UNICORNIO_RUN_SOURCE": "cron"}
        with tempfile.TemporaryDirectory() as diretorio:
            root = Path(diretorio)
            fonte = root / "editorial.input.json"
            fonte.write_text(json.dumps({
                "batch_id": "editorial-test",
                "posts": [{"post_id": 1, "cleaned_html": "<p>A</p>"}],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, ambiente, clear=False):
                generate_editorial_batch(
                    fonte, api_key="test-key", base_url=self.base,
                    model="editorial-test", root=root,
                )
            evento = json.loads(
                (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8").strip()
            )
            self.assertEqual(evento["event"], "editorial_model_request")
            self.assertEqual(evento["input_tokens"], 120)
            self.assertAlmostEqual(evento["model_cost_usd"], 0.000036, places=9)
            modulo = _guard()
            dados = modulo.editorial_direct_usage(
                root / "work" / "telemetry.jsonl", run_source="cron"
            )
        self.assertEqual(dados["requests"], 1)
        self.assertEqual(dados["prompt_tokens"], 120)
        self.assertAlmostEqual(dados["cost_usd"], 0.000036, places=9)


if __name__ == "__main__":
    unittest.main()
