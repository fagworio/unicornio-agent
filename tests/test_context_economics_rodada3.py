"""Testes de regressão da terceira rodada da auditoria de contexto.

Cenários pedidos na revisão:

* contrato REAL do SourceResolver x capacity (integração: resolver de verdade,
  mockando apenas a busca HTTP e o verifier);
* guard de tokens contando `prompt_tokens` (input + cache_read + cache_write);
* budget de contexto REALMENTE hard no `cards` (para antes de montar card);
* monitor imprime assinatura congelada quando o orçamento estoura (hash não muda);
* telemetria separando cron de manual (KPI oficial = fatia do cron);
* nomes precisos de tokens (prompt/output/total) e qualidade por decisão
  (auto/choose/reuse) para provar que a economia não piorou a imagem;
* `auto` com margem calibrável (EDITOR_AUTO_SCORE_MARGIN) e `score_gap` gravado.
"""

import argparse
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from unicornio_editor import session_budget
from unicornio_editor.config import Config
from unicornio_editor.observability import (
    append_telemetry,
    read_media_decision,
    read_telemetry_summary,
    record_media_decision,
    run_context,
)
from unicornio_editor.session_metrics import session_metrics

CRON_ENV = {"UNICORNIO_RUN_SOURCE": "cron",
            "HERMES_SESSION_ID": "cron_editorial_20260922_090923"}
MANUAL_ENV = {"UNICORNIO_RUN_SOURCE": "manual",
              "HERMES_SESSION_ID": "20260922_103317_904836"}


def _config(**overrides):
    values = {
        "content_source": "wordpress",
        "wordpress_url": "http://wp.test",
        "wordpress_api_base": "/wp-json/wp/v2",
        "dry_run": False,
    }
    values.update(overrides)
    return Config(**values)


class SourceResolverContractTests(unittest.TestCase):
    """O resolver REAL devolve `source_resolution="verified_page"`, não `valid`.

    Contar apenas `valid` deixava o teto de capacidade inerte em produção: o
    resolver seguia investigando candidato que já não era necessário. Estes
    testes usam o resolver de verdade e mockam só a BUSCA (HTTP) e o verificador.
    """

    @staticmethod
    def _candidato(url: str) -> dict:
        return {
            "direct_image_url": url,
            "source_page_url": "",
            "usable": False,
            "discovery_only": True,
            "rejected_reason": "missing_source_page",
            "engine": "yandex",
        }

    def test_capacity_defers_after_a_verified_page_from_the_real_resolver(self):
        from unicornio_editor import cli

        paginas = {
            "https://site/1": True,   # contém a imagem (verifier aprova)
            "https://site/2": True,
            "https://site/3": True,
        }
        resolvidas: list[str] = []

        def busca_falsa(query):
            # Uma página por query, na ordem das queries geradas pelo resolver.
            paginas_disponiveis = list(paginas)
            indice = min(len(resolvidas), len(paginas_disponiveis) - 1)
            return [{"source_page_url": paginas_disponiveis[indice]}]

        def verifier_falso(cand, **kwargs):
            pagina = cand.get("source_page_url")
            resolvidas.append(str(pagina))
            return {"valid": bool(paginas.get(pagina)), "reason": "ok", "images_in_page": 1}

        candidatos = [
            self._candidato("https://cdn/1.jpg"),
            self._candidato("https://cdn/2.jpg"),
            self._candidato("https://cdn/3.jpg"),
        ]
        with mock.patch(
            "unicornio_editor.media.source_resolver._buscador_padrao",
            side_effect=busca_falsa,
        ), mock.patch(
            "unicornio_editor.media.source_verify.validate_discovered_candidate",
            side_effect=verifier_falso,
        ), mock.patch(
            "unicornio_editor.media.evidence.dedupe_by_phash",
            side_effect=lambda aprovados, rejeitados: (aprovados, rejeitados),
        ):
            _aprovados, _rejeitados, deferidos = cli._enriquecer_candidatos(
                candidatos, subject="Metroid Prime 4", termo="metroid prime 4", capacity=1
            )
        # A capacidade era 1: assim que uma página foi VERIFICADA (contrato real
        # do resolver), os outros candidatos passaram a ser dispensados.
        self.assertEqual(len(deferidos), 2, [c.get("source_resolution") for c in candidatos])
        self.assertEqual(candidatos[0].get("source_resolution"), "verified_page")
        self.assertTrue(all(c.get("capacity_deferred") for c in deferidos))
        # Prova direta de que os dispensados NÃO foram investigados: o resolver
        # só roda nos candidatos sem página e deixa `candidate_pages` quando roda.
        self.assertNotIn("candidate_pages", candidatos[1])
        self.assertNotIn("candidate_pages", candidatos[2])


class HardSessionStopTests(unittest.TestCase):
    """Budget estourado => `cards` para ANTES de montar card (nada novo)."""

    def _run_cards(self, root: Path, config: Config) -> dict:
        from unicornio_editor import cli

        client = mock.Mock()
        with mock.patch.object(cli, "load_config", return_value=config), \
                mock.patch.object(cli, "WordPressClient", return_value=client), \
                mock.patch.object(
                    cli, "build_cards", return_value={"count": 5, "cards": [{"id": 1}]}
                ) as montar:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                cli.main(["cards", "--root", str(root), "--compact"])
        return json.loads(buffer.getvalue()), montar

    def test_context_budget_stops_cards_before_building_any_card(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(session_context_bytes_budget=100, max_posts_touched_per_run=5)
            session_budget.record_context_bytes(root, 500, config)  # estourou
            resultado, montar = self._run_cards(root, config)
        self.assertEqual(resultado["count"], 0)
        self.assertEqual(resultado["cards"], [])
        self.assertIn("budget de contexto", resultado["stop"])
        self.assertFalse(montar.called, "nao pode montar card depois do orcamento estourado")
        self.assertTrue(resultado["session"]["context_budget_exceeded"])

    def test_cards_still_work_below_the_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(session_context_bytes_budget=100000, max_posts_touched_per_run=5)
            resultado, montar = self._run_cards(root, config)
        self.assertTrue(montar.called)
        self.assertEqual(resultado["count"], 1)  # o mock devolve 1 card
        self.assertNotIn("stop", resultado)

    def test_touched_cap_stops_cards_before_building_any_card(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(max_posts_touched_per_run=1, session_context_bytes_budget=0)
            session_budget.record_touch(root, 10, config)
            resultado, montar = self._run_cards(root, config)
        self.assertEqual(resultado["count"], 0)
        self.assertIn("teto de posts tocados", resultado["stop"])
        self.assertFalse(montar.called)


class MonitorHashStabilityTests(unittest.TestCase):
    """O bloqueio de orçamento NÃO pode mudar a assinatura do monitor."""

    def _monitor(self) -> str:
        return (Path(__file__).parents[1] / "hermes" / "monitor.sh").read_text(encoding="utf-8")

    def test_blocked_budget_repeats_the_frozen_signature(self):
        conteudo = self._monitor()
        # Saida do bloqueio = ultima assinatura efetiva (arquivo), nunca o JSON
        # do guard (que muda a cada janela e acordaria o LLM justamente no freio).
        self.assertIn("monitor_effective_output", conteudo)
        self.assertIn("emitir_assinatura_congelada", conteudo)
        self.assertNotIn('printf \'%s\\n\' "BUDGET_EXHAUSTED', conteudo)
        self.assertIn("monitor-budget.log", conteudo)
        # O detalhe do guard vai para o LOG, nao para o stdout.
        self.assertIn('"$guard_out" >> "$BUDGET_LOG"', conteudo)

    def test_signature_is_saved_every_allowed_run(self):
        conteudo = self._monitor()
        self.assertIn('printf \'%s\\n\' "$out" > "$EFFECTIVE_FILE"', conteudo)


class PromptTokenGuardTests(unittest.TestCase):
    """O guard de tokens precisa contar cache-read/cache-write."""

    def _database(self, *, com_cache_write: bool = True) -> Path:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        path = Path(handle.name)
        db = sqlite3.connect(path)
        colunas = (
            "source TEXT, id TEXT, started_at INTEGER, estimated_cost_usd REAL, "
            "api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
            "cache_read_tokens INTEGER"
        )
        if com_cache_write:
            colunas += ", cache_write_tokens INTEGER"
        db.execute(f"CREATE TABLE sessions ({colunas})")
        valores = ("cron", "cron_editorial_20260922_090923", int(time.time()), 0.10,
                   60, 1000, 500, 40_000_000, 2_000_000)
        if not com_cache_write:
            valores = valores[:8]
        db.execute(
            "INSERT INTO sessions VALUES (" + ",".join("?" * len(valores)) + ")", valores
        )
        db.commit()
        db.close()
        self.addCleanup(path.unlink)
        return path

    def test_prompt_tokens_somam_input_cache_read_e_cache_write(self):
        from hermes.cost_guard import usage_measurement_in_last_24h

        medicao = usage_measurement_in_last_24h(self._database(), "editorial", "/project")
        self.assertEqual(medicao["input_tokens"], 1000)
        self.assertEqual(medicao["cache_read_tokens"], 40_000_000)
        self.assertEqual(medicao["cache_write_tokens"], 2_000_000)
        # input(1.000) + cache_read(40.000.000) + cache_write(2.000.000).
        self.assertEqual(medicao["prompt_tokens"], 42_001_000)

    def test_guard_bloqueia_por_prompt_tokens(self):
        import importlib.util

        caminho = Path(__file__).parents[1] / "hermes" / "cost_guard.py"
        spec = importlib.util.spec_from_file_location("cost_guard_mod", caminho)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)

        limites = {"cost_usd": 0.0, "requests": 0.0, "prompt_tokens": 1_000_000.0,
                   "context_bytes": 0.0}
        medidos = {"cost_usd": 0.10, "requests": 60, "prompt_tokens": 42_001_000}
        self.assertEqual(modulo._decision(limites, medidos), ("block", "prompt_tokens"))
        # O ponto da correção: UM MILHÃO de prompt tokens trip o limite, enquanto
        # o input novo (1.000) sozinho NÃO triparia — olhar só `input_tokens`
        # deixava o guard cego para o contexto relido do cache.
        self.assertEqual(
            modulo._decision({"prompt_tokens": 1_000_000.0}, {"prompt_tokens": 1000}),
            ("allow", "within_budget"),
        )
        self.assertEqual(
            modulo._decision({"prompt_tokens": 42_000_000.0}, medidos),
            ("block", "prompt_tokens"),
        )

    def test_banco_legado_sem_cache_write_continua_medindo(self):
        from hermes.cost_guard import usage_measurement_in_last_24h

        medicao = usage_measurement_in_last_24h(
            self._database(com_cache_write=False), "editorial", "/project"
        )
        self.assertEqual(medicao["cache_write_tokens"], 0)
        self.assertEqual(medicao["prompt_tokens"], 40_001_000)


class RunSourceIsolationTests(unittest.TestCase):
    """Cron x manual: o KPI oficial é a fatia do cron."""

    def test_run_context_classifica_pelo_id_da_sessao(self):
        with mock.patch.dict(os.environ, CRON_ENV, clear=False):
            self.assertEqual(run_context()["run_source"], "cron")
            self.assertEqual(run_context()["cron_job_id"], "editorial")
        with mock.patch.dict(os.environ, MANUAL_ENV, clear=False):
            self.assertEqual(run_context()["run_source"], "manual")
            self.assertEqual(run_context()["cron_job_id"], "")
        with mock.patch.dict(
            os.environ, {"HERMES_SESSION_ID": "", "UNICORNIO_RUN_SOURCE": ""}, clear=False
        ):
            self.assertEqual(run_context()["run_source"], "unknown")

    def test_run_context_usa_a_coluna_source_do_state_db(self):
        """O id da sessão do agente pode ser filho: o `source` do banco decide."""
        from unicornio_editor import observability

        with tempfile.TemporaryDirectory() as directory:
            banco = Path(directory) / "state.db"
            db = sqlite3.connect(banco)
            db.execute(
                "CREATE TABLE sessions (id TEXT, source TEXT, parent_session_id TEXT)"
            )
            db.executemany(
                "INSERT INTO sessions VALUES (?,?,?)",
                [
                    ("cron_9e39343dc6f5_20260922_090524", "cron", None),
                    # Sessão de FERRAMENTA dentro do cron: filha da sessão do job.
                    ("20260922_090601_abc123", "subagent",
                     "cron_9e39343dc6f5_20260922_090524"),
                    ("20260922_103317_904836", "cli", None),
                ],
            )
            db.commit()
            db.close()
            observability._RUN_CONTEXT_MEMO.clear()
            self.addCleanup(observability._RUN_CONTEXT_MEMO.clear)
            with mock.patch.dict(
                os.environ,
                {"HERMES_SESSION_ID": "20260922_090601_abc123",
                 "UNICORNIO_RUN_SOURCE": "", "HERMES_STATE_DB": str(banco),
                 "HERMES_EDITORIAL_CRON_JOB_ID": ""},
                clear=False,
            ):
                # Sessão FILHA de uma sessão de cron: além do run_source, o id do
                # JOB tem de sair da sessão RAIZ (senão o KPI fica sem job).
                self.assertEqual(run_context()["run_source"], "cron")
                self.assertEqual(run_context()["cron_job_id"], "9e39343dc6f5")
            observability._RUN_CONTEXT_MEMO.clear()
            with mock.patch.dict(
                os.environ,
                {"HERMES_SESSION_ID": "20260922_103317_904836",
                 "UNICORNIO_RUN_SOURCE": "", "HERMES_STATE_DB": str(banco),
                 "HERMES_EDITORIAL_CRON_JOB_ID": ""},
                clear=False,
            ):
                self.assertEqual(run_context()["run_source"], "manual")
                self.assertEqual(run_context()["cron_job_id"], "")

    def test_sem_state_db_cai_no_prefixo_do_id(self):
        from unicornio_editor import observability

        observability._RUN_CONTEXT_MEMO.clear()
        self.addCleanup(observability._RUN_CONTEXT_MEMO.clear)
        with mock.patch.dict(
            os.environ,
            {"HERMES_SESSION_ID": "cron_editorial_20260922_090923",
             "UNICORNIO_RUN_SOURCE": "", "HERMES_STATE_DB": "/nao/existe.db",
             "HERMES_EDITORIAL_CRON_JOB_ID": ""},
            clear=False,
        ):
            self.assertEqual(run_context()["run_source"], "cron")
            self.assertEqual(run_context()["cron_job_id"], "editorial")

    def test_session_id_desconhecido_cai_no_prefixo(self):
        """Id de sessão que não está no banco (ex.: CI) não vira "manual" sozinho."""
        from unicornio_editor import observability

        observability._RUN_CONTEXT_MEMO.clear()
        self.addCleanup(observability._RUN_CONTEXT_MEMO.clear)
        with mock.patch.dict(
            os.environ,
            {"HERMES_SESSION_ID": "cron_9e39343dc6f5_20260922_090524",
             "UNICORNIO_RUN_SOURCE": "", "HERMES_STATE_DB": "/nao/existe.db",
             "HERMES_EDITORIAL_CRON_JOB_ID": "9e39343dc6f5"},
            clear=False,
        ):
            self.assertEqual(run_context()["run_source"], "cron")
            self.assertEqual(run_context()["cron_job_id"], "9e39343dc6f5")

    def test_every_event_carries_the_run_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "apply_ready", post_id=1)
            linha = json.loads(
                (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8").strip()
            )
        self.assertEqual(linha["run_source"], "cron")
        self.assertEqual(linha["cron_job_id"], "editorial")
        self.assertIn("session_id", linha)

    def test_manual_execution_does_not_contaminate_the_official_kpi(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Cron: 1 READY com 2 KB de contexto.
            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                append_telemetry(root, "cmd_output", command="cards", bytes=2048, post_id=1)
                append_telemetry(root, "apply_ready", post_id=1)
            # Manual (verificação do operador): 50 KB que NÃO podem entrar no KPI.
            with mock.patch.dict(os.environ, MANUAL_ENV, clear=False):
                append_telemetry(root, "cmd_output", command="cards", bytes=50_000, post_id=1)
                append_telemetry(root, "cmd_output", command="content", bytes=30_000, post_id=1)

            with mock.patch.dict(os.environ, CRON_ENV, clear=False):
                metricas = session_metrics(
                    root, state_db=root / "inexistente.db", job_id="editorial",
                    project_root=str(root), hours=24,
                )
            completo = read_telemetry_summary(root, hours=24)
        self.assertEqual(metricas["telemetry"]["context_bytes_total"], 2048)
        # 1 READY na fatia do cron, 2 KB de contexto => 2 KB/READY.
        self.assertEqual(metricas["derived"]["tool_context_bytes_per_ready"], 2048.0)
        # A contaminação fica VISÍVEL, não escondida.
        self.assertEqual(completo["context_bytes_total"], 2048 + 80_000)
        self.assertEqual(completo["run_sources"]["manual"]["cmd_bytes"], 80_000)
        self.assertEqual(completo["run_sources"]["cron"]["cmd_bytes"], 2048)
        self.assertEqual(metricas["all_sources"]["context_bytes_total"], 82_048)


class DecisionQualityTests(unittest.TestCase):
    """Qualidade POR DECISÃO: auto/choose/reuse não podem piorar os gates."""

    def test_decision_ledger_and_cross_tab_by_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Busca `auto` no post 1 e `choose` no post 2.
            record_media_decision(root, 1, decision="auto", score_gap=4, query="q1")
            record_media_decision(root, 2, decision="choose", score_gap=0, query="q2")
            self.assertEqual(read_media_decision(root, 1)["decision"], "auto")
            self.assertEqual(read_media_decision(root, 1)["score_gap"], 4)
            self.assertEqual(read_media_decision(root, 99), {})

            append_telemetry(root, "media_search_result", post_id=1, decision="auto",
                             needed=2, reuse=0, strong=2, ambiguous=0, accepted=2,
                             rejected=0, deferred=0, examined=2, engines_queried=1)
            append_telemetry(root, "media_search_result", post_id=2, decision="choose",
                             needed=2, reuse=0, strong=1, ambiguous=1, accepted=2,
                             rejected=0, deferred=0, examined=3, engines_queried=2)
            append_telemetry(root, "media_validate_result", post_id=1, decision="auto",
                             valid=2, rejected_items=0, featured_status="passed")
            append_telemetry(root, "media_validate_result", post_id=2, decision="choose",
                             valid=1, rejected_items=1, featured_status="rejected")
            append_telemetry(root, "apply_ready", post_id=1, decision="auto", first_pass=True)
            append_telemetry(root, "apply_blocked", post_id=2, decision="choose",
                             first_pass=True, failure_reasons=["imagens_no_corpo"])
            resumo = read_telemetry_summary(root)
        qualidade = resumo["decision_quality"]
        self.assertEqual(qualidade["auto"]["searches"], 1)
        self.assertEqual(qualidade["auto"]["validate_rejected_items"], 0)
        self.assertEqual(qualidade["auto"]["first_pass_ready_rate"], 1.0)
        self.assertEqual(qualidade["auto"]["media_block_rate"], 0.0)
        self.assertEqual(qualidade["choose"]["validate_rejected_items"], 1)
        self.assertEqual(qualidade["choose"]["media_block_rate"], 1.0)
        # `choose` não teve READY nesta amostra: a taxa fica None (não existe
        # "primeira passada" sem um READY para medir) em vez de um 0.0 enganoso.
        self.assertIsNone(qualidade["choose"]["first_pass_ready_rate"])
        self.assertEqual(qualidade["choose"]["apply_ready"], 0)
        self.assertEqual(qualidade["choose"]["apply_media_blocks"], 1)

    def test_workflow_attaches_decision_to_apply_events(self):
        from unicornio_editor.workflow import _decision_fields

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 7, decision="reuse", score_gap=None)
            campos = _decision_fields(root, 7)
            self.assertEqual(campos, {"decision": "reuse"})
            self.assertEqual(_decision_fields(root, 8), {})
            self.assertEqual(_decision_fields(root, None), {})


class AutoMarginCalibrationTests(unittest.TestCase):
    """A margem do `auto` é parâmetro de calibração, não fato estabelecido."""

    @staticmethod
    def _forte(url: str, score: int) -> dict:
        return {
            "direct_image_url": url,
            "source_page_url": "https://pagina/" + url.rsplit("/", 1)[-1],
            "evidence_score": score,
            "evidence": {"verdict": "deterministic_match"},
            "official_source": "",
            "already_in_library": False,
            "needs_vision": False,
        }

    def test_margem_default_manda_empate_para_julgamento(self):
        from unicornio_editor.cli import _media_decision

        decisao = _media_decision(
            [self._forte("https://a/1.jpg", 12), self._forte("https://a/2.jpg", 10)]
        )
        self.assertEqual(decisao["decision"], "auto")  # gap 2 = margem default
        self.assertEqual(decisao["score_gap"], 2)

    def test_margem_maior_exige_diferenca_maior(self):
        from unicornio_editor.cli import _media_decision

        decisao = _media_decision(
            [self._forte("https://a/1.jpg", 12), self._forte("https://a/2.jpg", 10)],
            margin=3,
        )
        self.assertEqual(decisao["decision"], "choose")
        self.assertIn("gap 2 < margem 3", decisao["reason"])

    def test_config_expoe_a_margem(self):
        import os as _os

        from unicornio_editor.config import load_config

        with mock.patch.dict(
            _os.environ,
            {"WORDPRESS_URL": "http://wp.test", "EDITOR_AUTO_SCORE_MARGIN": "4"},
            clear=True,
        ):
            self.assertEqual(load_config().auto_score_margin, 4)
        with mock.patch.dict(_os.environ, {"WORDPRESS_URL": "http://wp.test"}, clear=True):
            self.assertEqual(load_config().auto_score_margin, 2)


if __name__ == "__main__":
    unittest.main()
