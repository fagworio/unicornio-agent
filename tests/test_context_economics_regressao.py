"""Testes de regressão da segunda rodada da auditoria de contexto.

Cobrem exatamente os cenários pedidos na revisão:

* teto de sessão: A tocado, B tocado, C `session_budget_exhausted` SEM escrita, e
  C liberado numa sessão NOVA (ledger expirado);
* concorrência: vários PROCESSOS reservando vaga ao mesmo tempo não podem passar
  do teto nem perder gravação;
* mídia: acervo local cobre o subject -> `reuse`, zero engines e zero visão;
  1 candidato forte -> `auto` (sem julgamento); 2 próximos -> `choose` (visão
  permitida); candidato dispensado por capacidade NÃO conta como rejeitado;
* `requires_content=false` impedindo de verdade a leitura do corpo;
* métricas de mídia por READY (`local_reuse_rate`, `web_searches_per_ready`,
  `vision_calls_per_ready`, `candidates_examined_per_ready`).
"""

import argparse
import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from unicornio_editor import session_budget
from unicornio_editor.config import Config
from unicornio_editor.observability import append_telemetry, read_telemetry_summary
from unicornio_editor.session_metrics import session_metrics


def _config(**overrides):
    values = {
        "content_source": "wordpress",
        "wordpress_url": "http://wp.test",
        "wordpress_api_base": "/wp-json/wp/v2",
        "dry_run": False,
    }
    values.update(overrides)
    return Config(**values)


def _claim_worker(payload):
    """Worker de outro PROCESSO (o cenário real de cron + execução manual)."""
    root, post_id, cap = payload
    from unicornio_editor import session_budget as budget
    from unicornio_editor.config import Config as Cfg

    config = Cfg(
        "wordpress", "http://wp.test", "/wp-json/wp/v2",
        dry_run=False, max_posts_touched_per_run=cap,
    )
    permitido, _ = budget.claim_touch(root, post_id, config)
    return permitido


class SessionCapFlowTests(unittest.TestCase):
    """run 1: A tocado, B tocado, C bloqueado (sem escrita). run 2: C liberado."""

    def _apply(self, root: Path, post_id: int, config: Config):
        from unicornio_editor import cli

        patch_file = root / "patch.json"
        patch_file.write_text(json.dumps({"cleaned_html": "<p>x</p>"}), encoding="utf-8")
        with mock.patch.object(cli, "load_config", return_value=config), mock.patch.object(
            cli, "WordPressClient", return_value=mock.Mock()
        ), mock.patch.object(
            cli, "apply_editorial",
            return_value={"post_id": post_id, "status": "ready", "wordpress_changed": True},
        ) as applied:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                cli.main(["apply", str(post_id), str(patch_file), "--root", str(root), "--compact"])
        return json.loads(buffer.getvalue()), applied

    def test_third_post_is_refused_without_any_write_and_new_session_allows_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(max_posts_touched_per_run=2, session_window_minutes=90)

            resultado_a, aplicado_a = self._apply(root, 201, config)
            resultado_b, aplicado_b = self._apply(root, 202, config)
            self.assertEqual(resultado_a["status"], "ready")
            self.assertEqual(resultado_b["status"], "ready")
            self.assertTrue(aplicado_a.called and aplicado_b.called)

            # Post C: recusado pelo teto, SEM chamar o apply (nenhuma escrita).
            resultado_c, aplicado_c = self._apply(root, 203, config)
            self.assertEqual(resultado_c["status"], "session_budget_exhausted")
            self.assertFalse(resultado_c["wordpress_changed"])
            self.assertFalse(aplicado_c.called, "o post C nao pode ser tocado")
            self.assertEqual(
                session_budget.status(root, config)["posts_touched"], [201, 202]
            )

            # Nova execucao do cron (ledger expirado): C e permitido.
            depois = session_budget.status(root, config)["started_at"]
            self.assertTrue(depois)
            import time

            futuro = time.time() + (91 * 60)
            permitido, projecao = session_budget.claim_touch(root, 203, config, now=futuro)
            self.assertTrue(permitido)
            self.assertIn(203, projecao["posts_touched"])
            self.assertEqual(projecao["posts_touched_count"], 1)  # sessao nova

            resultado_c2, aplicado_c2 = self._apply(root, 203, config)
            self.assertEqual(resultado_c2["status"], "ready")
            self.assertTrue(aplicado_c2.called)


class SessionCapConcurrencyTests(unittest.TestCase):
    """Dois processos não podem ler `touched=1`, gravar `2` e liberar um terceiro."""

    def test_concurrent_processes_never_exceed_the_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(max_posts_touched_per_run=2)
            with ProcessPoolExecutor(max_workers=6) as pool:
                permitidos = list(
                    pool.map(_claim_worker, [(str(root), pid, 2) for pid in range(300, 306)])
                )
            self.assertEqual(sum(1 for p in permitidos if p), 2, permitidos)
            self.assertEqual(len(session_budget.status(root, config)["posts_touched"]), 2)

    def test_concurrent_processes_do_not_lose_touches(self):
        """Sem lock, dois processos gravam a MESMA lista e um post some."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(max_posts_touched_per_run=0)  # teto desligado
            with ProcessPoolExecutor(max_workers=6) as pool:
                list(pool.map(_claim_worker, [(str(root), pid, 0) for pid in range(400, 406)]))
            tocados = session_budget.status(root, config)["posts_touched"]
            self.assertEqual(sorted(tocados), [400, 401, 402, 403, 404, 405])


class MediaFlowRegressionTests(unittest.TestCase):
    """reuse/auto/choose e a separação entre deferido e rejeitado."""

    @staticmethod
    def _candidate(url, score, verdict="deterministic_match"):
        return {
            "direct_image_url": url,
            "source_page_url": f"https://pagina/{url.rsplit('/', 1)[-1]}",
            "evidence_score": score,
            "evidence": {"verdict": verdict},
            "official_source": "",
            "already_in_library": False,
            "needs_vision": verdict == "ambiguous",
        }

    def test_local_reuse_covers_subject_without_web_and_without_vision(self):
        """Media Library cobre o subject -> reuse, engines 0, visão 0."""
        from unicornio_editor import cli

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work").mkdir(parents=True, exist_ok=True)
            (root / "work" / "media_index.json").write_text(
                json.dumps({"entries": [
                    {"phash": "a", "source_url": "https://cdn/1.jpg",
                     "source_page": "https://pagina/1", "subject": "",
                     "media_id": 501},
                    {"phash": "b", "source_url": "https://cdn/2.jpg",
                     "source_page": "https://pagina/2", "subject": "",
                     "media_id": 502},
                ]}),
                encoding="utf-8",
            )
            client = mock.Mock()
            client.search_media.return_value = [{"id": 501}, {"id": 502}]
            reuso = cli._reuse_from_library(client, root, "Redfall", limit=2)
            self.assertEqual(len(reuso), 2)

            # Nenhuma engine é consultada quando o déficit já é zero.
            from unicornio_editor.media import search as busca

            with mock.patch.object(busca, "search_web_images") as web:
                compacto = cli._compact_media_search(
                    query="Redfall", subject="Redfall", needed=2, reuso=reuso,
                    aprovados=[], rejeitados=[], engines=[], audit="",
                )
            self.assertFalse(web.called)
            self.assertEqual(compacto["decision"], "reuse")
            self.assertEqual(compacto["engines_queried"], [])
            self.assertEqual(compacto["capacity"]["missing"], 0)

            # E nenhum evento de visão é emitido por esse caminho.
            resumo = read_telemetry_summary(root)
            self.assertEqual(resumo["media_economy"]["vision_calls"], 0)

    def test_single_strong_candidate_is_auto_and_needs_no_judgement(self):
        from unicornio_editor.cli import _compact_media_search, _media_decision

        abaixo = [self._candidate("https://a/1.jpg", 12)]
        decisao = _media_decision(abaixo)
        self.assertEqual(decisao["decision"], "auto")
        self.assertIn("unico candidato forte", decisao["reason"])
        self.assertFalse(decisao["select"]["needs_vision"])
        compacto = _compact_media_search(
            query="q", subject="s", needed=1, reuso=[], aprovados=abaixo,
            rejeitados=[], engines=["bing"], audit="",
        )
        self.assertEqual(compacto["decision"], "auto")
        self.assertEqual(len(compacto["options"]), 1)
        self.assertIn("decision_reason", compacto)

    def test_two_close_candidates_go_to_judgement_with_vision_allowed(self):
        from unicornio_editor.cli import _media_decision

        decisao = _media_decision([
            self._candidate("https://a/1.jpg", 10),
            self._candidate("https://a/2.jpg", 10),
        ])
        self.assertEqual(decisao["decision"], "choose")
        self.assertIsNone(decisao["select"])
        self.assertEqual(len(decisao["options"]), 2)
        self.assertIn("empate", decisao["reason"])

    def test_ambiguous_candidates_keep_vision_available(self):
        from unicornio_editor.cli import _media_decision

        decisao = _media_decision([
            self._candidate("https://a/1.jpg", 5, "ambiguous"),
            self._candidate("https://a/2.jpg", 4, "ambiguous"),
        ])
        self.assertEqual(decisao["decision"], "choose")
        self.assertTrue(all(opcao["needs_vision"] for opcao in decisao["options"]))
        self.assertIn("visao decide", decisao["reason"])

    def test_deferred_candidates_are_not_counted_as_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(
                root, "media_search_result", query="q", needed=2, needed_web=2,
                reuse=0, strong=2, ambiguous=0, accepted=2, rejected=1,
                deferred=3, examined=6, engines_queried=1, decision="auto",
            )
            resumo = read_telemetry_summary(root)
        economia = resumo["media_economy"]
        self.assertEqual(economia["rejected_total"], 1)
        self.assertEqual(economia["deferred_total"], 3)   # separado!
        self.assertEqual(economia["examined_total"], 6)
        self.assertEqual(economia["local_reuse_rate"], 0.0)
        self.assertEqual(economia["searches_with_web"], 1)

    def test_media_metrics_per_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(
                root, "media_search_result", query="q1", needed=2, needed_web=0,
                reuse=2, strong=0, ambiguous=0, accepted=0, rejected=0,
                deferred=0, examined=0, engines_queried=0, decision="reuse",
            )
            append_telemetry(
                root, "media_search_result", query="q2", needed=2, needed_web=2,
                reuse=0, strong=2, ambiguous=1, accepted=3, rejected=2,
                deferred=1, examined=4, engines_queried=2, decision="auto",
            )
            append_telemetry(root, "vision_call", scope="featured_preflight", cached=False)
            append_telemetry(root, "apply_ready", post_id=1)
            append_telemetry(root, "apply_ready", post_id=2)
            metricas = session_metrics(root, state_db=root / "inexistente.db", hours=24)
        derivadas = metricas["derived"]
        self.assertEqual(derivadas["local_reuse_rate"], 0.5)          # 2 de 4
        self.assertEqual(derivadas["web_searches_per_ready"], 0.5)    # 1 de 2
        self.assertEqual(derivadas["vision_calls_per_ready"], 0.5)    # 1 de 2
        self.assertEqual(derivadas["candidates_examined_per_ready"], 2.0)
        self.assertEqual(metricas["telemetry"]["media_economy"]["deferred_total"], 1)


class ContentGuardTests(unittest.TestCase):
    """`requires_content=false` vale até o fim: o corpo não entra na conversa."""

    def _blocked_post(self, root: Path, gates: list[str]) -> None:
        directory = root / "backups" / "42"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "editorial.blocked.json").write_text(
            json.dumps({"blocked_checklist": {"items": [
                {"name": gate, "status": "fail"} for gate in gates
            ]}}),
            encoding="utf-8",
        )

    def _run_content(self, root: Path, post_id: int, *, force: bool, state: str):
        from unicornio_editor import cli

        argv = ["content", str(post_id), "--root", str(root)]
        if force:
            argv.append("--force")
        client = mock.Mock()
        client.get_post.return_value = {"id": post_id, "status": "pending", "meta": {"_hermes_state": state}}
        with mock.patch.object(cli, "load_config", return_value=_config(dry_run=True)), \
                mock.patch.object(cli, "WordPressClient", return_value=client), \
                mock.patch.object(
                    cli, "get_cleaned_content",
                    return_value={"post_id": post_id, "cleaned_html": "<p>artigo inteiro</p>"},
                ) as conteudo:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                cli.main(argv)
        return json.loads(buffer.getvalue()), conteudo

    def test_media_rework_does_not_read_the_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._blocked_post(root, ["imagens_no_corpo"])
            resultado, conteudo = self._run_content(root, 42, force=False, state="blocked")
        self.assertEqual(resultado["status"], "content_not_required")
        self.assertFalse(conteudo.called, "o corpo nao pode ser lido num rework de midia")
        self.assertEqual(resultado["component"], ["media"])
        self.assertIn("--for-fix", resultado["action"])

    def test_force_reads_the_body_when_the_agent_decides_to_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._blocked_post(root, ["imagens_no_corpo"])
            resultado, conteudo = self._run_content(root, 42, force=True, state="blocked")
        self.assertTrue(conteudo.called)
        self.assertEqual(resultado["cleaned_html"], "<p>artigo inteiro</p>")

    def test_text_rework_keeps_the_body_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._blocked_post(root, ["qualidade_texto"])
            resultado, conteudo = self._run_content(root, 42, force=False, state="blocked")
        self.assertTrue(conteudo.called)
        self.assertIn("cleaned_html", resultado)

    def test_post_out_of_blocked_state_is_not_gated(self):
        """Humano deu `retry`: o gate de rework não vale mais para leitura."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._blocked_post(root, ["imagens_no_corpo"])
            resultado, conteudo = self._run_content(root, 42, force=False, state="new")
        self.assertTrue(conteudo.called)


class ReadyMetricsWindowTests(unittest.TestCase):
    """A janela do telemetry precisa bater com a janela do gasto (state.db)."""

    def test_media_metrics_use_the_same_window_as_production(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(root, "apply_ready", post_id=1)
            append_telemetry(
                root, "media_search_result", query="q", needed=1, needed_web=1,
                reuse=0, strong=1, ambiguous=0, accepted=1, rejected=0,
                deferred=0, examined=1, engines_queried=1, decision="auto",
            )
            banco = root / "state.db"
            db = sqlite3.connect(banco)
            db.execute(
                "CREATE TABLE sessions (id TEXT, source TEXT, started_at INTEGER, "
                "estimated_cost_usd REAL, api_call_count INTEGER, input_tokens INTEGER, "
                "cache_read_tokens INTEGER)"
            )
            import time

            db.execute(
                "INSERT INTO sessions VALUES ('cron_editorial_20260922_090923','cron',?,0.10,40,1000,9000)",
                (int(time.time()),),
            )
            db.commit()
            db.close()
            metricas = session_metrics(
                root, state_db=banco, job_id="editorial", project_root=str(root), hours=24
            )
        self.assertEqual(metricas["derived"]["web_searches_per_ready"], 1.0)
        self.assertEqual(metricas["derived"]["tool_context_bytes_per_ready"] is not None, True)


class CliParserRegressionTests(unittest.TestCase):
    def test_content_command_has_force_flag(self):
        from unicornio_editor.cli import build_parser

        args = build_parser().parse_args(["content", "42"])
        self.assertFalse(args.force)
        args = build_parser().parse_args(["content", "42", "--force"])
        self.assertTrue(args.force)


if __name__ == "__main__":
    unittest.main()
