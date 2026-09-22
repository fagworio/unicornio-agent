"""Testes da auditoria de contexto (economia de sessão/telemetria/contratos).

Cobrem as mudanças P0/P1/P2 que NÃO afrouxam nenhum gate de qualidade: hard cap
de posts tocados, busca adaptativa por déficit, seleção determinística,
Media Library primeiro, memo do enriquecimento, draft parcial + merge, contratos
compactos, telemetria por post/sessão e orçamento de contexto.
"""

import io
import json
import sqlite3
import tempfile
import unittest
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
        "dry_run": True,
    }
    values.update(overrides)
    return Config(**values)


class SessionBudgetTests(unittest.TestCase):
    """P0: teto de posts TOCADOS (hard cap) + budget de contexto da sessão."""

    def test_new_post_is_blocked_after_the_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(max_posts_touched_per_run=2)
            self.assertEqual(session_budget.status(root, config)["remaining_posts"], 2)
            for post_id in (11, 12):
                permitido, _ = session_budget.touch_allowed(root, post_id, config)
                self.assertTrue(permitido)
                session_budget.record_touch(root, post_id, config)
            permitido, projecao = session_budget.touch_allowed(root, 13, config)
            self.assertFalse(permitido)
            self.assertEqual(projecao["remaining_posts"], 0)
            self.assertEqual(projecao["posts_touched_count"], 2)

    def test_same_post_is_always_allowed_inside_the_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(max_posts_touched_per_run=1)
            session_budget.record_touch(root, 7, config)
            permitido, _ = session_budget.touch_allowed(root, 7, config)
            self.assertTrue(permitido)

    def test_zero_disables_the_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(max_posts_touched_per_run=0)
            for post_id in range(1, 8):
                session_budget.record_touch(root, post_id, config)
            permitido, projecao = session_budget.touch_allowed(root, 99, config)
            self.assertTrue(permitido)
            self.assertIsNone(projecao["remaining_posts"])

    def test_ledger_expires_by_inactivity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(max_posts_touched_per_run=2, session_window_minutes=10)
            session_budget.record_touch(root, 5, config, now=1000.0)
            # Mesma sessão (dentro da janela): o post continua contando.
            self.assertEqual(
                session_budget.status(root, config, now=1100.0)["posts_touched_count"], 1
            )
            # Fora da janela: o cron seguinte começa com o teto zerado.
            self.assertEqual(
                session_budget.status(root, config, now=1000.0 + 11 * 60)["posts_touched_count"], 0
            )

    def test_ready_counter_and_stop_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(max_posts_touched_per_run=1, target_ready_per_run=5)
            projecao = session_budget.record_touch(root, 8, config, ready=True)
            self.assertEqual(projecao["ready"], 1)
            self.assertEqual(projecao["target_ready"], 5)
            motivo = session_budget.stop_reason(root, config)
            self.assertIn("teto de posts tocados", motivo)

    def test_context_budget_ends_the_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(session_context_bytes_budget=1000)
            session_budget.record_context_bytes(root, 600, config)
            self.assertEqual(session_budget.stop_reason(root, config), "")
            projecao = session_budget.record_context_bytes(root, 500, config)
            self.assertTrue(projecao["context_budget_exceeded"])
            self.assertIn("budget de contexto", session_budget.stop_reason(root, config))

    def test_broken_ledger_never_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work").mkdir()
            (root / "work" / "session_state.json").write_text("{nao e json", encoding="utf-8")
            config = _config()
            self.assertEqual(session_budget.status(root, config)["posts_touched_count"], 0)

    def test_publish_commands_do_not_extend_the_editorial_ledger(self):
        """Publicação roda em OUTRO cron no mesmo diretório.

        Se o contexto do `publish-ready` entrasse no ledger, a janela se
        estenderia e a execução seguinte de editorial herdaria o teto esgotado.
        """
        import argparse

        from unicornio_editor.cli import _record_cmd_output

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(root=root)
            args.command = "publish-ready"
            _record_cmd_output(args, {"published": 2})
            config = _config()
            self.assertEqual(session_budget.status(root, config)["context_bytes_used"], 0)
            args.command = "cards"
            _record_cmd_output(args, {"count": 1, "cards": []})
            self.assertGreater(session_budget.status(root, config)["context_bytes_used"], 0)


class ApplySessionCapTests(unittest.TestCase):
    """P0: o apply é o guardião do teto (nenhum post NOVO acima da conta)."""

    def _run_apply(self, root: Path, post_id: int, config: Config) -> dict:
        from unicornio_editor import cli

        patch_file = root / "patch.json"
        patch_file.write_text(json.dumps({"cleaned_html": "<p>x</p>"}), encoding="utf-8")
        client = mock.Mock()
        with mock.patch.object(cli, "load_config", return_value=config), mock.patch.object(
            cli, "WordPressClient", return_value=client
        ), mock.patch.object(
            cli, "apply_editorial", return_value={"post_id": post_id, "status": "ready",
                                                  "wordpress_changed": True}
        ) as applied:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = cli.main(
                    ["apply", str(post_id), str(patch_file), "--root", str(root), "--compact"]
                )
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertTrue(applied.called or True)
        return json.loads(buffer.getvalue()), applied

    def test_refuses_new_post_above_the_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(dry_run=False, max_posts_touched_per_run=2)
            session_budget.record_touch(root, 100, config)
            session_budget.record_touch(root, 101, config)
            result, applied = self._run_apply(root, 102, config)
            self.assertEqual(result["status"], "session_budget_exhausted")
            self.assertFalse(result["wordpress_changed"])
            self.assertFalse(applied.called)  # NADA foi escrito
            self.assertIn("nenhum comando novo", result["action"])

    def test_rework_of_touched_post_still_applies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(dry_run=False, max_posts_touched_per_run=1)
            session_budget.record_touch(root, 100, config)
            result, applied = self._run_apply(root, 100, config)
            self.assertEqual(result["status"], "ready")
            self.assertTrue(applied.called)

    def test_apply_counts_the_touched_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(dry_run=False, max_posts_touched_per_run=2)
            result, _ = self._run_apply(root, 55, config)
            self.assertEqual(result["session"]["posts_touched"], [55])
            self.assertEqual(result["session"]["ready"], 1)


class MediaSelectionTests(unittest.TestCase):
    """P1: o modelo só decide quando existe ambiguidade real."""

    @staticmethod
    def _candidate(url: str, score: int, verdict: str = "deterministic_match"):
        return {
            "direct_image_url": url,
            "source_page_url": f"https://pagina/{url.rsplit('/', 1)[-1]}",
            "evidence_score": score,
            "evidence": {"verdict": verdict},
            "official_source": "dominio oficial",
            "already_in_library": False,
            "needs_vision": verdict == "ambiguous",
        }

    def test_single_strong_candidate_is_selected_automatically(self):
        from unicornio_editor.cli import _media_decision

        decisao = _media_decision(
            [self._candidate("https://a/1.jpg", 12), self._candidate("https://a/2.jpg", 5, "ambiguous")]
        )
        self.assertEqual(decisao["decision"], "auto")
        self.assertEqual(decisao["select"]["url"], "https://a/1.jpg")
        self.assertEqual(len(decisao["options"]), 1)

    def test_tie_sends_two_to_three_options_for_judgement(self):
        from unicornio_editor.cli import _media_decision

        decisao = _media_decision(
            [
                self._candidate("https://a/1.jpg", 10),
                self._candidate("https://a/2.jpg", 10),
                self._candidate("https://a/3.jpg", 9),
            ]
        )
        self.assertEqual(decisao["decision"], "choose")
        self.assertIsNone(decisao["select"])
        self.assertEqual(len(decisao["options"]), 3)

    def test_no_candidate_reports_none(self):
        from unicornio_editor.cli import _media_decision

        self.assertEqual(_media_decision([])["decision"], "none")

    def test_compact_output_hides_raw_objects_and_summarises_rejections(self):
        from unicornio_editor.cli import _compact_media_search

        rejeitados = [
            {"direct_image_url": "https://x/1.jpg", "evidence": {"verdict": "unresolved_source"}},
            {"direct_image_url": "https://x/2.jpg", "evidence": {"verdict": "source_mismatch"}},
            {"direct_image_url": "https://x/3.jpg", "evidence": {"verdict": "source_mismatch"}},
        ]
        resultado = _compact_media_search(
            query="redfall xbox",
            subject="Redfall",
            needed=2,
            reuso=[],
            aprovados=[self._candidate("https://a/1.jpg", 12), self._candidate("https://a/2.jpg", 11)],
            rejeitados=rejeitados,
            engines=["bing"],
            audit="work/search/redfall-1.json",
        )
        self.assertEqual(resultado["capacity"]["strong"], 2)
        self.assertEqual(resultado["capacity"]["missing"], 0)
        self.assertIn("ambiguous", resultado["capacity"])
        self.assertEqual(
            resultado["rejected_summary"], {"unresolved_source": 1, "source_mismatch": 2}
        )
        self.assertEqual(resultado["rejected_total"], 3)
        self.assertNotIn("rejected", resultado)
        serializado = json.dumps(resultado)
        self.assertNotIn("phash", serializado)
        for campo in ("url", "source", "score", "verdict", "official_source",
                      "already_in_library", "needs_vision"):
            self.assertIn(campo, resultado["options"][0])

    def test_missing_capacity_points_to_the_next_action(self):
        from unicornio_editor.cli import _compact_media_search

        resultado = _compact_media_search(
            query="obra", subject="Obra", needed=2, reuso=[],
            aprovados=[self._candidate("https://a/1.jpg", 12)],
            rejeitados=[], engines=["bing"], audit="",
        )
        self.assertEqual(resultado["capacity"]["missing"], 1)
        self.assertIn("faltam 1", resultado["action"])

    def test_reuse_alone_covers_the_deficit(self):
        from unicornio_editor.cli import _compact_media_search

        resultado = _compact_media_search(
            query="obra", subject="Obra", needed=2,
            reuso=[{"url": "https://a/1.jpg", "source": "https://pagina/1", "media_id": 9}],
            aprovados=[self._candidate("https://a/2.jpg", 12)], rejeitados=[],
            engines=[], audit="",
        )
        self.assertEqual(resultado["capacity"]["reuse"], 1)
        self.assertEqual(resultado["capacity"]["missing"], 0)
        self.assertIn("reuse", resultado)
        # O acervo local cobriu o déficit: nao ha julgamento a fazer.
        self.assertEqual(resultado["decision"], "reuse")


class LibraryFirstTests(unittest.TestCase):
    """P1: Media Library/índice local ANTES da web."""

    def _write_index(self, root: Path, entries: list[dict]) -> None:
        (root / "work").mkdir(parents=True, exist_ok=True)
        (root / "work" / "media_index.json").write_text(
            json.dumps({"entries": entries}), encoding="utf-8"
        )

    def test_reuse_returns_verified_entries_with_original_url(self):
        from unicornio_editor.cli import _reuse_from_library

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_index(root, [{
                "phash": "abc", "source_url": "https://cdn/original.jpg",
                "source_page": "https://pagina/original", "subject": "Redfall",
                "media_id": 77, "uses": 1,
            }])
            client = mock.Mock()
            reuso = _reuse_from_library(client, root, "Redfall", limit=2)
        self.assertEqual(len(reuso), 1)
        self.assertEqual(reuso[0]["url"], "https://cdn/original.jpg")
        self.assertEqual(reuso[0]["source"], "https://pagina/original")
        self.assertEqual(reuso[0]["media_id"], 77)

    def test_reuse_skips_entries_without_provenance(self):
        from unicornio_editor.cli import _reuse_from_library

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_index(root, [{
                "phash": "abc", "source_url": "https://cdn/original.jpg",
                "source_page": "", "subject": "Redfall", "media_id": 77,
            }])
            reuso = _reuse_from_library(mock.Mock(), root, "Redfall", limit=2)
        self.assertEqual(reuso, [])

    def test_reuse_skips_removed_attachment(self):
        from unicornio_editor.cli import _reuse_from_library

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_index(root, [{
                "phash": "abc", "source_url": "https://cdn/original.jpg",
                "source_page": "https://pagina/original", "subject": "Redfall",
                "media_id": 77,
            }])
            client = mock.Mock()
            client.get_media.side_effect = RuntimeError("404")
            client.search_media.return_value = []
            reuso = _reuse_from_library(client, root, "Redfall", limit=2)
        self.assertEqual(reuso, [])

    def test_reuse_finds_legacy_entries_through_the_media_library(self):
        """Entradas antigas não têm subject, mas a Media Library acha a imagem."""
        from unicornio_editor.cli import _reuse_from_library

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_index(root, [{
                "phash": "abc", "source_url": "https://cdn/legado.jpg",
                "source_page": "https://pagina/legado", "subject": "",  # legado
                "media_id": 114361,
            }])
            client = mock.Mock()
            client.search_media.return_value = [{"id": 114361, "title": "Redfall key art"}]
            reuso = _reuse_from_library(client, root, "Redfall", limit=2)
        self.assertEqual(len(reuso), 1)
        self.assertEqual(reuso[0]["url"], "https://cdn/legado.jpg")
        self.assertEqual(reuso[0]["media_id"], 114361)

    def test_reuse_ignores_media_without_indexed_provenance(self):
        from unicornio_editor.cli import _reuse_from_library

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_index(root, [])
            client = mock.Mock()
            client.search_media.return_value = [{"id": 999, "title": "Redfall"}]
            reuso = _reuse_from_library(client, root, "Redfall", limit=2)
        self.assertEqual(reuso, [])


class EnrichmentMemoTests(unittest.TestCase):
    """P1: sem processamento duplicado (memo) e sem investigar o que não falta."""

    def _candidate(self, url: str) -> dict:
        return {
            "direct_image_url": url,
            "source_page_url": "https://pagina/x",
            "usable": True,
            "engine": "bing",
        }

    def test_memo_avoids_reprocessing_the_same_candidate(self):
        from unicornio_editor import cli

        chamadas = []

        def fake_validate(cand, **kwargs):
            chamadas.append(cand["direct_image_url"])
            return {"valid": True, "reason": "ok", "images_in_page": 1}

        memo: dict = {}
        vereditos = []
        with mock.patch(
            "unicornio_editor.media.source_verify.validate_discovered_candidate",
            side_effect=fake_validate,
        ), mock.patch(
            "unicornio_editor.media.evidence.dedupe_by_phash",
            side_effect=lambda aprovados, rejeitados: (aprovados, rejeitados),
        ):
            for _ in range(2):
                aprovados, rejeitados = cli._enriquecer_candidatos(
                    [self._candidate("https://a/1.jpg")],
                    subject="Redfall",
                    termo="redfall",
                    enriched_cache=memo,
                )
                candidato = (aprovados + rejeitados)[0]
                vereditos.append((candidato.get("evidence") or {}).get("verdict"))
        self.assertEqual(chamadas, ["https://a/1.jpg"])  # processado UMA vez
        self.assertTrue(memo, "o memo precisa guardar o resultado enriquecido")
        self.assertIsNotNone(vereditos[0])
        # O segundo enriquecimento reusa o memo: nenhuma verificacao nova e a
        # MESMA decisao (o memo e o resultado, nao um atalho).
        self.assertEqual(vereditos[0], vereditos[1])

    def test_capacity_stops_investigating_unneeded_candidates(self):
        from unicornio_editor import cli

        resolvidos = []

        def fake_resolver(cand, subject, verifier=None):
            resolvidos.append(cand["direct_image_url"])
            return {
                **cand,
                "source_page_url": "https://pagina/resolvida",
                "valid": True,
            }

        candidatos = [
            {"direct_image_url": "https://a/1.jpg", "source_page_url": "", "usable": False},
            {"direct_image_url": "https://a/2.jpg", "source_page_url": "", "usable": False},
        ]
        with mock.patch(
            "unicornio_editor.media.source_resolver.resolve_candidate_source",
            side_effect=fake_resolver,
        ), mock.patch(
            "unicornio_editor.media.source_verify.validate_discovered_candidate",
            return_value={"valid": True, "reason": "ok", "images_in_page": 1},
        ), mock.patch(
            "unicornio_editor.media.evidence.dedupe_by_phash",
            side_effect=lambda aprovados, rejeitados: (aprovados, rejeitados),
        ):
            _aprovados, rejeitados = cli._enriquecer_candidatos(
                candidatos, subject="Redfall", termo="redfall", capacity=1
            )
        self.assertEqual(resolvidos, ["https://a/1.jpg"])  # o 2º não foi investigado
        deferidos = [c for c in rejeitados if c.get("capacity_deferred")]
        self.assertEqual(len(deferidos), 1)
        self.assertEqual(deferidos[0]["evidence"]["verdict"], "capacity_met")
        self.assertFalse(deferidos[0]["evidence"]["needs_vision"])


class DraftPatchTests(unittest.TestCase):
    """P0/P2: rework recebe só o componente; patch parcial mescla no draft."""

    def _draft(self, root: Path, post_id: int = 42) -> Path:
        directory = root / "backups" / str(post_id)
        directory.mkdir(parents=True, exist_ok=True)
        draft = {
            "site_relevance": {"decision": "process", "confidence": 0.99},
            "cleaned_html": "<p>artigo inteiro</p>",
            "seo": {"title": "Redfall key art", "focus_keyword": "redfall"},
            "media_plan": [{"paragraph_index": 1, "direct_image_url": "https://a/1.jpg"}],
            "needs_trailer": False,
        }
        (directory / "editorial.draft.json").write_text(
            json.dumps(draft), encoding="utf-8"
        )
        return directory

    def test_merge_replaces_lists_and_merges_dicts(self):
        from unicornio_editor.cli import _merge_patch

        mesclado = _merge_patch(
            {"seo": {"title": "antigo", "focus_keyword": "k"}, "media_plan": [{"a": 1}]},
            {"seo": {"title": "novo"}, "media_plan": [{"b": 2}, {"c": 3}], "_nota": "x"},
        )
        self.assertEqual(mesclado["seo"], {"title": "novo", "focus_keyword": "k"})
        self.assertEqual(mesclado["media_plan"], [{"b": 2}, {"c": 3}])
        self.assertNotIn("_nota", mesclado)

    def test_merge_with_draft_writes_the_audit_file(self):
        from unicornio_editor.cli import _merge_patch_with_draft

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._draft(root)
            mesclado, nota = _merge_patch_with_draft(
                root, 42, {"media_plan": [{"direct_image_url": "https://a/2.jpg"}]}
            )
        self.assertEqual(mesclado["media_plan"], [{"direct_image_url": "https://a/2.jpg"}])
        self.assertEqual(mesclado["seo"]["title"], "Redfall key art")  # do draft
        self.assertIn("media_plan", nota)

    def test_merge_rejects_empty_patch(self):
        from unicornio_editor.cli import _merge_patch_with_draft

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._draft(root)
            with self.assertRaises(ValueError):
                _merge_patch_with_draft(root, 42, {"_nota": "nada util"})

    def test_for_fix_returns_only_the_media_component_and_the_error(self):
        from unicornio_editor.cli import _draft_for_fix

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            directory_post = self._draft(root)
            (directory_post / "editorial.blocked.json").write_text(
                json.dumps({
                    "blocked_reason": "imagens_no_corpo",
                    "blocked_checklist": {"items": [
                        {"name": "imagens_no_corpo", "status": "fail",
                         "detail": "900 palavras exigem >= 4 imagens; conteudo tem 2"},
                        {"name": "seo_keyword", "status": "pass"},
                    ]},
                }),
                encoding="utf-8",
            )
            from unicornio_editor.workflow import load_draft

            extrato = _draft_for_fix(root, 42, load_draft(root, 42), ["media"])
        self.assertEqual(extrato["component"], "media")
        self.assertEqual(extrato["media_plan"], [{"paragraph_index": 1, "direct_image_url": "https://a/1.jpg"}])
        self.assertFalse(extrato["requires_content"])
        self.assertEqual(extrato["subject"], "Redfall key art")
        self.assertEqual(extrato["error"]["failed"][0]["name"], "imagens_no_corpo")
        # O artigo inteiro NÃO vai para o LLM no rework de mídia.
        self.assertNotIn("cleaned_html", extrato)
        self.assertIn("full_draft", extrato)

    def test_blocked_components_maps_gates_to_components(self):
        from unicornio_editor.cli import _blocked_components

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            directory_post = self._draft(root)
            (directory_post / "editorial.blocked.json").write_text(
                json.dumps({"blocked_checklist": {"items": [
                    {"name": "qualidade_texto", "status": "fail"},
                    {"name": "trailer_youtube", "status": "fail"},
                    {"name": "imagens_no_corpo", "status": "pass"},
                ]}}),
                encoding="utf-8",
            )
            self.assertEqual(_blocked_components(root, 42), ["text", "trailer"])


class MediaValidateCompactTests(unittest.TestCase):
    """P1: contrato explícito do media-validate."""

    def test_contract_reports_capacity_and_featured(self):
        from unicornio_editor.cli import _compact_media_validate

        editorial = {
            "seo": {"title": "Redfall"},
            "cleaned_html": "<p>" + ("palavra " * 700) + "</p>",
            "media_plan": [
                {"direct_image_url": "https://a/1.jpg"},
                {"direct_image_url": "https://a/2.jpg"},
                {"direct_image_url": "https://a/3.jpg", "is_featured": True},
            ],
        }
        resultado = _compact_media_validate(
            editorial,
            {
                "valid": 2,
                "rejected": [{"index": 1, "reason": "duplicado"}],
                "featured_vision": [{"status": "passed", "reason": "MATCH 0.9", "candidates": [1, 2]}],
                "listicle": {"applicable": False},
            },
            audit="work/media-validate/1.json",
        )
        self.assertEqual(resultado["capacity"]["required"], 4)  # 700 palavras
        self.assertEqual(resultado["capacity"]["valid"], 1)     # 2 itens, 1 rejeitado
        self.assertEqual(resultado["capacity"]["missing"], 3)
        self.assertEqual(resultado["rejected"], [{"index": 1, "reason": "duplicado"}])
        self.assertEqual(resultado["featured"]["status"], "passed")
        self.assertNotIn("candidates", resultado["featured"])

    def test_absent_featured_is_reported(self):
        from unicornio_editor.cli import _compact_media_validate

        resultado = _compact_media_validate(
            {"seo": {"title": "X"}, "cleaned_html": "<p>curto</p>", "media_plan": []},
            {"valid": 0, "rejected": [], "listicle": {}},
            audit="",
        )
        self.assertEqual(resultado["featured"]["status"], "absent")


class TelemetryTests(unittest.TestCase):
    """P0/P1: telemetria por comando, por post e por sessão."""

    def test_summary_breaks_context_down_by_post_and_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(root, "cmd_output", command="cards", kind="read",
                             bytes=2000, post_id=11)
            append_telemetry(root, "cmd_output", command="apply", kind="write",
                             bytes=500, post_id=11, cleaned_html_bytes=4000)
            append_telemetry(root, "cmd_output", command="media-validate", kind="read",
                             bytes=300, post_id=12)
            append_telemetry(root, "apply_ready", post_id=11)
            resumo = read_telemetry_summary(root)
        self.assertEqual(resumo["context_bytes_by_post"]["11"], 2500)
        self.assertEqual(resumo["post_context_detail"]["11"],
                         {"cards": 2000, "apply": 500})
        self.assertEqual(resumo["context_bytes_by_kind"]["read"], 2300)
        self.assertEqual(resumo["context_bytes_by_kind"]["write"], 500)
        self.assertEqual(resumo["context_bytes_per_ready"], 2800)
        self.assertEqual(resumo["production"]["unique_touched_posts"], 1)

    def test_session_metrics_correlates_tokens_with_production(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(root, "cmd_output", command="cards", bytes=4000, post_id=11)
            append_telemetry(root, "apply_ready", post_id=11)
            append_telemetry(root, "apply_ready", post_id=12)
            banco = root / "state.db"
            db = sqlite3.connect(banco)
            db.execute(
                "CREATE TABLE sessions (source TEXT, started_at INTEGER, "
                "estimated_cost_usd REAL, cron_job_id TEXT, api_call_count INTEGER, "
                "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER)"
            )
            import time

            db.execute(
                "INSERT INTO sessions VALUES ('cron', ?, 0.50, 'editorial', 40, 1000, 200, 9000)",
                (int(time.time()),),
            )
            db.commit()
            db.close()
            metricas = session_metrics(
                root, state_db=banco, job_id="editorial", project_root=str(root), hours=24
            )
        self.assertEqual(metricas["telemetry"]["ready"], 2)
        self.assertEqual(metricas["hermes_sessions"]["requests"], 40)
        self.assertEqual(metricas["derived"]["tokens_per_ready"], 5000.0)
        self.assertEqual(metricas["derived"]["requests_per_ready"], 20.0)
        self.assertEqual(metricas["derived"]["tool_context_bytes_per_ready"], 2000.0)
        self.assertEqual(metricas["derived"]["cost_per_ready_usd"], 0.25)

    def test_session_metrics_without_attribution_does_not_invent_zeros(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(root, "apply_ready", post_id=1)
            banco = root / "state.db"
            db = sqlite3.connect(banco)
            db.execute(
                "CREATE TABLE sessions (source TEXT, started_at INTEGER, "
                "estimated_cost_usd REAL, cron_job_id TEXT)"
            )
            db.commit()
            db.close()
            metricas = session_metrics(
                root, state_db=banco, job_id="", project_root=str(root), hours=24
            )
        self.assertIsNone(metricas["hermes_sessions"])
        self.assertIsNone(metricas["derived"]["tokens_per_ready"])
        self.assertEqual(metricas["telemetry"]["ready"], 1)


class AttributionTests(unittest.TestCase):
    """A medição precisa achar a sessão do cron no schema REAL do Hermes.

    No schema atual não existe coluna de job e as sessões de cron gravam ``cwd``
    NULL — sem o prefixo do id (``cron_<job_id>_<data>_<hora>``) a medição ficava
    "indisponível" e um teto que nunca mede nunca bloqueia.
    """

    def _database(self, rows: list[tuple]) -> Path:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        path = Path(handle.name)
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE sessions (id TEXT, source TEXT, started_at INTEGER, "
            "estimated_cost_usd REAL, cwd TEXT, api_call_count INTEGER, "
            "input_tokens INTEGER, cache_read_tokens INTEGER)"
        )
        db.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        db.commit()
        db.close()
        self.addCleanup(path.unlink)
        return path

    def test_cost_guard_attributes_by_session_id_prefix(self):
        import time

        from hermes.cost_guard import usage_measurement_in_last_24h

        agora = int(time.time())
        banco = self._database([
            ("cron_editorial_20260922_090923", "cron", agora, 0.20, None, 40, 1000, 9000),
            ("cron_outrojob_20260922_090000", "cron", agora, 0.80, None, 90, 5000, 7000),
        ])
        medicao = usage_measurement_in_last_24h(banco, "editorial", "/project")
        self.assertEqual(medicao["cost_usd"], 0.20)
        self.assertEqual(medicao["runs"], 1)  # o outro job não entra
        self.assertEqual(medicao["input_tokens"], 1000)
        self.assertIn("prefixo do id", medicao["scope"])

    def test_session_metrics_uses_the_same_attribution(self):
        import time

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._database_and_telemetry(root, int(time.time()))
            metricas = session_metrics(
                root, state_db=root / "state.db", job_id="editorial",
                project_root="/project", hours=24,
            )
        self.assertEqual(metricas["hermes_sessions"]["requests"], 40)
        self.assertEqual(metricas["hermes_sessions"]["input_tokens"], 1000)
        self.assertEqual(metricas["derived"]["requests_per_ready"], 20.0)

    def _database_and_telemetry(self, root: Path, agora: int) -> None:
        append_telemetry(root, "apply_ready", post_id=1)
        append_telemetry(root, "apply_ready", post_id=2)
        db = sqlite3.connect(root / "state.db")
        db.execute(
            "CREATE TABLE sessions (id TEXT, source TEXT, started_at INTEGER, "
            "estimated_cost_usd REAL, cwd TEXT, api_call_count INTEGER, "
            "input_tokens INTEGER, cache_read_tokens INTEGER)"
        )
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("cron_editorial_20260922_090923", "cron", agora, 0.30, None, 40, 1000, 9000),
        )
        db.commit()
        db.close()

    def test_telemetry_summary_respects_the_window(self):
        from unicornio_editor.observability import telemetry_path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = telemetry_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            antigo = "2026-01-01T00:00:00+00:00"
            path.write_text(
                json.dumps({"event": "apply_ready", "ts": antigo, "post_id": 1}) + "\n"
                + json.dumps({"event": "apply_ready", "post_id": 2}) + "\n",
                encoding="utf-8",
            )
            sem_janela = read_telemetry_summary(root)
            com_janela = read_telemetry_summary(root, hours=24)
        self.assertEqual(sem_janela["production"]["unique_ready_posts"], 2)
        # O evento fora da janela não infla a métrica de 24h.
        self.assertEqual(com_janela["production"]["unique_ready_posts"], 1)


class FixPlanRequiresContentTests(unittest.TestCase):
    """P2: o card diz se o rework precisa do artigo inteiro."""

    def _blocked_dir(self, root: Path, gates: list[str]) -> Path:
        directory = root / "backups" / "42"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "editorial.blocked.json").write_text(
            json.dumps({"blocked_checklist": {"items": [
                {"name": gate, "status": "fail"} for gate in gates
            ]}}),
            encoding="utf-8",
        )
        return directory

    def test_media_only_rework_does_not_require_content(self):
        from unicornio_editor.workflow import _fix_plan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fix = _fix_plan(self._blocked_dir(root, ["imagens_no_corpo"]),
                            {"missing": 2}, {"action": "ok"}, True)
        self.assertFalse(fix["requires_content"])

    def test_text_rework_requires_content(self):
        from unicornio_editor.workflow import _fix_plan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fix = _fix_plan(self._blocked_dir(root, ["qualidade_texto"]),
                            {"missing": 0}, {"action": "ok"}, True)
        self.assertTrue(fix["requires_content"])


if __name__ == "__main__":
    unittest.main()
