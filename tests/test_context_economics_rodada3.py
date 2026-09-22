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
    """Capacidade = forte + frame DISTINTO (não `verified_page` só).

    `verified_page` prova que a imagem está naquela página, não que ela é
    relevante para o subject (evidence_score vem depois) nem que o frame é
    distinto (pHash vem depois). Estes testes usam o resolver REAL e mockam só a
    busca HTTP, o verificador e os hashes.
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

    def _rodar(self, candidatos, *, capacity, hashes=None, fortes=()):
        from unicornio_editor import cli

        contador = {"n": 0}
        fortes = tuple(fortes)

        def busca_falsa(_query):
            contador["n"] += 1
            indice = min(contador["n"] - 1, len(candidatos) - 1)
            return [{"source_page_url": f"https://site/pagina{indice + 1}"}]

        def verifier_falso(cand, **kwargs):
            return {"valid": True, "reason": "ok", "images_in_page": 1}

        def hashes_falsos(urls):
            return {u: (hashes or {}).get(u, f"hash-{u}") for u in urls if u}

        def evidence_dirigida(subject, *, filename="", **kwargs):
            # Marca como FORTE só os candidatos pedidos: o teste isola a lógica de
            # CAPACIDADE (forte + frame distinto), sem depender da heurística de
            # score. `filename` identifica o candidato (basename da URL).
            forte = any(marcador in filename for marcador in fortes)
            return {
                "subject": subject, "matched": ["filename"] if forte else [],
                "evidence": {}, "local_score": 9 if forte else 0,
                "score": 9 if forte else 0, "gate": "relevance", "penalties": [],
                "needs_vision": False,
                "verdict": "deterministic_match" if forte else "reject",
            }

        with mock.patch(
            "unicornio_editor.media.source_resolver._buscador_padrao",
            side_effect=busca_falsa,
        ), mock.patch(
            "unicornio_editor.media.source_verify.validate_discovered_candidate",
            side_effect=verifier_falso,
        ), mock.patch(
            "unicornio_editor.media.visual_hash.image_hashes",
            side_effect=hashes_falsos,
        ), mock.patch(
            "unicornio_editor.media.evidence.evidence_score",
            side_effect=evidence_dirigida,
        ), mock.patch(
            "unicornio_editor.media.evidence.dedupe_by_phash",
            side_effect=lambda aprovados, rejeitados, **kw: (aprovados, rejeitados),
        ):
            return cli._enriquecer_candidatos(
                candidatos, subject="Metroid Prime 4", termo="metroid prime 4",
                capacity=capacity,
            )

    def test_candidato_fraco_nao_consome_capacidade(self):
        """Página verificada + evidência FRACA não pode parar a investigação.

        Antes da rodada 4, o candidato 1 (origem resolvida) consumia a capacidade
        e os candidatos 2 e 3 eram dispensados — mesmo sendo rejeitados por
        relevância logo em seguida.
        """
        candidatos = [
            self._candidato("https://cdn/aaa.jpg"),
            self._candidato("https://cdn/bbb.jpg"),
            self._candidato("https://cdn/ccc.jpg"),
        ]
        aprovados, rejeitados, deferidos = self._rodar(candidatos, capacity=1)
        self.assertEqual(deferidos, [], "não pode dispensar sem ter forte distinto")
        self.assertEqual(aprovados, [])
        # Todos foram INVESTIGADOS (têm páginas de origem resolvidas).
        self.assertTrue(all(c.get("candidate_pages") for c in candidatos))

    def test_forte_e_distinto_atende_a_capacidade(self):
        candidatos = [
            self._candidato("https://cdn/metroid-prime-4-keyart.jpg"),
            self._candidato("https://cdn/bbb.jpg"),
            self._candidato("https://cdn/ccc.jpg"),
        ]
        aprovados, _rejeitados, deferidos = self._rodar(
            candidatos, capacity=1, fortes=["metroid-prime-4-keyart"]
        )
        fortes = [
            c for c in aprovados
            if (c.get("evidence") or {}).get("verdict") == "deterministic_match"
        ]
        self.assertEqual(len(fortes), 1)
        self.assertEqual(len(deferidos), 2)
        self.assertTrue(all(c.get("capacity_deferred") for c in deferidos))
        # Os dispensados não foram investigados.
        self.assertFalse(candidatos[1].get("candidate_pages"))
        self.assertFalse(candidatos[2].get("candidate_pages"))

    def test_mesmo_frame_nao_conta_como_dois_fortes(self):
        """Capacidade 2 com 2 copias do MESMO frame: a 3ª ainda é investigada."""
        candidatos = [
            self._candidato("https://cdn/metroid-prime-4-keyart.jpg"),
            self._candidato("https://espelho/metroid-prime-4-keyart.jpg"),
            self._candidato("https://cdn/metroid-prime-4-outra.jpg"),
        ]
        hashes = {
            "https://cdn/metroid-prime-4-keyart.jpg": "MESMO-FRAME",
            "https://espelho/metroid-prime-4-keyart.jpg": "MESMO-FRAME",
            "https://cdn/metroid-prime-4-outra.jpg": "FRAME-DIFERENTE",
        }
        _aprovados, _rejeitados, deferidos = self._rodar(
            candidatos, capacity=2, hashes=hashes,
            fortes=["metroid-prime-4-keyart", "metroid-prime-4-outra"],
        )
        self.assertEqual(deferidos, [])
        self.assertTrue(candidatos[2].get("candidate_pages"), "3ª não pode ser dispensada")


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
        self.assertEqual(qualidade["auto"]["first_pass_success_rate"], 1.0)
        self.assertEqual(qualidade["auto"]["media_block_rate"], 0.0)
        self.assertEqual(qualidade["choose"]["validate_rejected_items"], 1)
        self.assertEqual(qualidade["choose"]["media_block_rate"], 1.0)
        # `choose` não teve READY: 0 de 1 primeira tentativa = 0.0 (é a taxa de
        # SUCESSO de primeira tentativa; antes a métrica ficava None e escondia o
        # fracasso).
        self.assertEqual(qualidade["choose"]["first_pass_success_rate"], 0.0)
        self.assertEqual(qualidade["choose"]["first_pass_attempts"], 1)
        self.assertEqual(qualidade["choose"]["first_pass_blocked"], 1)
        self.assertIsNone(qualidade["choose"]["ready_first_pass_share"])
        self.assertEqual(qualidade["choose"]["apply_ready"], 0)
        self.assertEqual(qualidade["choose"]["apply_media_blocks"], 1)

    def test_workflow_attaches_decision_to_apply_events(self):
        from unicornio_editor.workflow import _decision_fields

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision_id = record_media_decision(root, 7, decision="reuse", score_gap=None)
            # Plano AUSENTE com histórico "reuse": a atribuição FALTA e `decision`
            # NÃO é emitido (a decisão da busca anterior não escolheu imagem
            # nenhuma do plano final — não pode entrar em decision_quality).
            campos = _decision_fields(root, 7)
            self.assertEqual(campos["decision_attribution"], "missing")
            self.assertNotIn("decision", campos)
            self.assertEqual(campos["decision_unattributed"], "reuse")
            # Com o plano TOTALMENTE rastreado, o rótulo volta a sair do ledger.
            campos = _decision_fields(root, 7, [{"decision_id": decision_id}])
            self.assertEqual(campos["decision"], "reuse")
            self.assertEqual(campos["decision_attribution"], "resolved")
            self.assertEqual(campos["decision_id"] if "decision_id" in campos else "", "")
            self.assertEqual(_decision_fields(root, 8), {})
            self.assertEqual(_decision_fields(root, None), {})

    def test_plano_com_decision_id_por_item_manda_na_atribuicao(self):
        """O resultado do apply é atribuído à decisão de CADA imagem."""
        from unicornio_editor.workflow import _decision_fields

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Ledger tem a ÚLTIMA decisão como "auto"; o plano diz que a imagem do
            # item 1 veio de "choose" — o plano vence.
            record_media_decision(root, 7, decision="auto", score_gap=4)
            campos = _decision_fields(
                root, 7,
                [
                    {"decision_id": "aaa", "decision": "choose"},
                    {"decision_id": "bbb", "decision": "auto"},
                ],
            )
            self.assertEqual(campos["decision_ids"], ["aaa", "bbb"])
            # Plano MISTO: sem rótulo único (atribuir a um deles seria chute).
            self.assertNotIn("decision", campos)

    def test_plano_com_decisao_unica_rotula_o_evento(self):
        from unicornio_editor.workflow import _decision_fields

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # O rótulo vem do LEDGER (pelo id), não do texto do plano.
            record_media_decision(root, 7, decision="choose", decision_id="aaa")
            campos = _decision_fields(
                root, 7, [{"decision_id": "aaa", "decision": "auto"}]  # texto MENTE
            )
            self.assertEqual(campos["decision"], "choose")
            self.assertEqual(campos["decision_ids"], ["aaa"])
            self.assertEqual(campos["decision_attribution"], "resolved")


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
