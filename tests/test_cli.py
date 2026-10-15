"""Testes dos modos compactos do CLI (economia de tokens).

As projecoes compactas seguem a regra: SUCCESS = minimo de informacao;
FAILURE = somente o necessario para corrigir. O relatorio completo fica em
arquivo (apply.latest.json) para auditoria.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from unicornio_editor.cli import (
    _compact_apply,
    _compact_cards,
    _compact_checklist,
    _compact_queue,
    _monitor_line,
    _record_cmd_output,
)


class CompactOutputTests(unittest.TestCase):
    def test_compact_cards_keeps_only_the_next_action(self):
        result = _compact_cards({"cards": [{
            "id": 9, "title": "Post", "state": "BLOCKED", "attempts": 2,
            "retry_mode": "uncertain_second_pass", "seo_exists": True, "images": {"missing": 2}, "featured": {"action": "replace"},
            "game_hint": "Jogo", "blocked": True, "blocked_reason": "imagens_no_corpo",
            "fix": {"find_inline_images": 2}, "draft": "work/drafts/9.json",
            "word_count": 900, "content": "nao deve aparecer",
        }]})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["cards"][0]["fix"], {"find_inline_images": 2})
        self.assertEqual(result["cards"][0]["retry_mode"], "uncertain_second_pass")
        self.assertNotIn("word_count", result["cards"][0])
        self.assertNotIn("content", result["cards"][0])

    def test_compact_cards_exposes_partial_progress_and_fix(self):
        result = _compact_cards({"cards": [{
            "id": 114559, "title": "Post parcial", "state": "partial", "attempts": 0,
            "retry_mode": None, "seo_exists": True,
            "images": {"required": 6, "valid": 4, "missing": 2},
            "featured": {"action": "ok"}, "game_hint": True,
            "blocked": False, "partial": True,
            "partial_progress": {"required": 6, "completed": 4, "missing": 2},
            "fix": {"find_inline_images": 2, "featured": "ok"},
        }]})
        card = result["cards"][0]
        self.assertTrue(card["partial"])
        self.assertEqual(card["partial_progress"]["missing"], 2)
        self.assertEqual(card["fix"]["find_inline_images"], 2)

    def test_compact_apply_success_is_minimal(self):
        result = _compact_apply(
            {
                "post_id": 1,
                "wordpress_changed": True,
                "featured_media": 7,
                "checklist": {
                    "items": [{"name": "backup", "status": "pass"}],
                    "all_passed": True,
                },
                "media_plan_results": [
                    {"media_id": 10},
                    {"media_id": 11},
                    {"status": "rejected", "detail": "x"},
                ],
                "content_preview": "<p>texto gigante que nao deve vazar</p>",
                "trailer": {"video_id": "abc"},
            }
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["checklist"], "pass")
        self.assertEqual(result["media"], {"accepted": 2, "rejected": 1})
        self.assertEqual(result["featured_media"], 7)
        self.assertNotIn("failed", result)
        # Nunca retorna o que o agente nao precisa decidir agora.
        self.assertNotIn("content_preview", result)
        self.assertNotIn("trailer", result)
        self.assertNotIn("checklist_items", result)

    def test_compact_apply_needs_rework_lists_only_failures(self):
        result = _compact_apply(
            {
                "post_id": 2,
                "wordpress_changed": False,
                "status": "needs_rework",
                "blocked_reasons": ["imagens_no_corpo"],
                "blocked_detail": "esperado 4, encontrado 2",
                "checklist": {
                    "items": [
                        {
                            "name": "imagens_no_corpo",
                            "status": "fail",
                            "detail": "esperado 4, encontrado 2",
                        }
                    ]
                },
            }
        )
        self.assertEqual(result["status"], "needs_rework")
        self.assertEqual(
            result["failed"],
            [{"name": "imagens_no_corpo", "detail": "esperado 4, encontrado 2"}],
        )

    def test_compact_apply_dry_run_reports_checklist(self):
        result = _compact_apply(
            {
                "post_id": 3,
                "wordpress_changed": False,
                "dry_run": True,
                "checklist": {
                    "items": [{"name": "imagens_no_corpo", "status": "fail", "detail": "0/4"}]
                },
                "media_plan_results": [],
            }
        )
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["checklist"], "fail")
        self.assertEqual(result["failed"], [{"name": "imagens_no_corpo", "detail": "0/4"}])

    def test_compact_apply_uncertain_is_minimal(self):
        result = _compact_apply(
            {
                "post_id": 4,
                "wordpress_changed": False,
                "status": "uncertain",
                "skip_reason": "conteudo irrelevante",
            }
        )
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["skip_reason"], "conteudo irrelevante")

    def test_compact_apply_preserves_partial_media_progress_for_zero_of_two(self):
        result = _compact_apply({
            "post_id": 114829, "status": "partial", "state": "partial",
            "wordpress_changed": False, "processing_passes": 2,
            "no_progress_attempts": 1,
            "partial": {"required": 2, "completed": 0, "missing": 2},
            "checklist": {"items": [{"name": "imagens_no_corpo", "status": "fail", "detail": "0/2"}]},
        })
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["state"], "partial")
        self.assertTrue(result["state_changed"])
        self.assertFalse(result["content_changed"])
        self.assertEqual(result["partial"], {"required": 2, "completed": 0, "missing": 2})
        self.assertEqual(result["processing_passes"], 2)
        self.assertEqual(result["no_progress_attempts"], 1)
        self.assertEqual(result["checklist"], "fail")
        self.assertTrue(result["failed"])

    def test_compact_apply_preserves_partial_media_progress_for_zero_of_four(self):
        result = _compact_apply({
            "post_id": 114849, "status": "partial", "state": "partial",
            "wordpress_changed": False,
            "partial": {"required": 4, "completed": 0, "missing": 4},
            "processing_passes": 1, "no_progress_attempts": 0,
            "checklist": {"items": [{"name": "imagens_no_corpo", "status": "fail", "detail": "0/4"}]},
        })
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["partial"]["missing"], 4)
        self.assertFalse(result["content_changed"])

    def test_compact_apply_reports_persisted_baseline_for_skipped_post(self):
        result = _compact_apply(
            {
                "post_id": 4,
                "wordpress_changed": True,
                "baseline_enriched": True,
                "status": "skipped",
                "skip_reason": "conteudo irrelevante",
            }
        )
        self.assertTrue(result["wordpress_changed"])
        self.assertTrue(result["baseline_enriched"])

    def test_monitor_line_idle_is_zero(self):
        # Sem trabalho elegivel a linha e "0" (estavel; nao acorda o agente).
        self.assertEqual(
            _monitor_line({"eligible_rework_ids": [], "unprocessed_ids": [], "posts": []}),
            "0",
        )

    def test_monitor_line_encodes_cooldown_not_wall_clock(self):
        # Rework em cooldown e codificado por next_retry_at (id@minuto), nao
        # por bucket de parede: a linha so muda quando o cooldown expira.
        report = {
            "eligible_rework_ids": [],
            "unprocessed_ids": [],
            "posts": [
                {
                    "id": 42,
                    "state": "blocked",
                    "next_retry_at": "2026-08-25T14:30:00+00:00",
                }
            ],
        }
        self.assertEqual(_monitor_line(report), "42@2026-08-25T14:30")

    def test_monitor_line_includes_eligible_and_new(self):
        report = {
            "eligible_rework_ids": [2],
            "unprocessed_ids": [9],
            "posts": [],
        }
        self.assertEqual(_monitor_line(report), "2 9")

    def test_monitor_line_wakes_for_old_pending(self):
        # Backlog antigo (>7d, fora de recent_unprocessed_ids) TAMBEM acorda o
        # agente: a linha do monitor usa unprocessed_ids (todo pending nao
        # processado), nao so os recentes — o backlog nao pode morrer de fome.
        report = {
            "eligible_rework_ids": [],
            "unprocessed_ids": [101416],
            "recent_unprocessed_ids": [],
            "posts": [],
        }
        self.assertEqual(_monitor_line(report), "101416")

    def test_compact_checklist_failure_only(self):
        ok = _compact_checklist({"items": [{"name": "a", "status": "pass"}]})
        self.assertEqual(ok, {"status": "pass", "failed": []})
        bad = _compact_checklist(
            {
                "items": [
                    {"name": "a", "status": "pass"},
                    {"name": "b", "status": "fail", "detail": "detalhe b"},
                    {"name": "c", "status": "error", "detail": "detalhe c"},
                ]
            }
        )
        self.assertEqual(bad["status"], "fail")
        self.assertEqual(
            bad["failed"],
            [
                {"name": "b", "detail": "detalhe b"},
                {"name": "c", "detail": "detalhe c"},
            ],
        )


    def test_record_cmd_output_covers_read_and_write_commands(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        class NS:
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = NS()
            args.root = root
            args.command = "cards"
            _record_cmd_output(args, {"count": 1, "cards": []})
            # P0 da auditoria de contexto: o apply TAMBEM coloca contexto (a
            # decisao do proximo passo sai do resultado dele) e agora e medido,
            # com kind=write, post_id e o tamanho do cleaned_html.
            args.command = "apply"
            args.post_id = 77
            _record_cmd_output(
                args,
                {"status": "ready", "cleaned_html": "<p>" + "x" * 100 + "</p>"},
            )
            # media-validate (leitura pesada) entrou na medicao.
            args.command = "media-validate"
            args.post_id = 88
            _record_cmd_output(args, {"valid": 1, "rejected": [], "listicle": {}})
            from unicornio_editor.observability import read_telemetry_summary

            summary = read_telemetry_summary(root)
            self.assertEqual(summary["by_event"].get("cmd_output"), 3)
            self.assertGreater(summary["context_bytes_total"], 0)
            self.assertIn("cards", summary["context_bytes_by_command"])
            self.assertIn("apply", summary["context_bytes_by_command"])
            self.assertIn("media-validate", summary["context_bytes_by_command"])
            self.assertIn("write", summary["context_bytes_by_kind"])
            self.assertEqual(summary["context_bytes_by_post"]["77"],
                             summary["context_bytes_by_command"]["apply"])


    def test_retry_all_parser_defaults_states(self):
        from unicornio_editor.cli import build_parser

        args = build_parser().parse_args(["retry-all", "--root", "."])
        self.assertEqual(args.command, "retry-all")
        self.assertEqual(args.states, "awaiting_human,blocked")

    def test_retry_all_parser_custom_states(self):
        from unicornio_editor.cli import build_parser

        args = build_parser().parse_args(["retry-all", "--states", "blocked", "--root", "."])
        self.assertEqual(args.states, "blocked")

    def test_v2_media_recovery_parser_is_read_only_by_default(self):
        from unicornio_editor.cli import build_parser

        args = build_parser().parse_args([
            "v2-recover-media", "115152", "115305", "--evidence", "evidence.json",
        ])
        self.assertEqual(args.command, "v2-recover-media")
        self.assertFalse(args.apply)

        with self.assertRaises(SystemExit):
            build_parser().parse_args([
                "v2-recover-media", "115152", "115305", "--evidence", "evidence.json",
                "--legacy-verified",
            ])

    def test_v2_media_adoption_parser_requires_explicit_confirmation(self):
        from unicornio_editor.cli import build_parser

        args = build_parser().parse_args([
            "v2-adopt-media", "115152", "115305", "--evidence", "evidence.json",
            "--authorization-id", "ticket-1", "--authorized-by", "editor",
            "--authorization-reason", "approved for article", "--confirm-adoption",
        ])
        self.assertEqual(args.command, "v2-adopt-media")
        self.assertTrue(args.confirm_adoption)
        self.assertFalse(hasattr(args, "apply"))

        with self.assertRaises(SystemExit):
            build_parser().parse_args([
                "v2-adopt-media", "115152", "115305", "--evidence", "evidence.json",
                "--authorization-id", "ticket-1", "--authorized-by", "editor",
                "--authorization-reason", "approved", "--confirm-adoption", "--apply",
            ])

    def test_media_search_listicle_parser_accepts_quoted_titles(self):
        from unicornio_editor.cli import build_parser

        args = build_parser().parse_args(
            ["media-search-listicle", "Blue Box temporada 2", "Psyren anime", "--limit", "2"]
        )
        self.assertEqual(args.command, "media-search-listicle")
        self.assertEqual(args.titulos, ["Blue Box temporada 2", "Psyren anime"])
        self.assertEqual(args.limit, 2)

    def test_compact_queue_drops_redundant_fields(self):
        # A projecao compacta mantem o resumo (contagens/ids) e reduz cada post
        # aos campos essenciais, cortando os ~15 campos redundantes do modo cheio.
        report = {
            "pending": 2,
            "ready_ids": [10],
            "unprocessed_ids": [20],
            "posts": [
                {
                    "id": 10,
                    "date": "2026-08-27",
                    "date_gmt": "2026-08-27T03:00:00",
                    "word_count": 2000,
                    "state": "ready",
                    "attempts": 0,
                    "next_retry_at": "",
                    "last_error": "" + ("x" * 200),
                    "prepared": True,
                    "edited": True,
                    "blocked": False,
                    "uncertain": False,
                    "awaiting_human": False,
                    "skipped": False,
                    "title": "Post A",
                }
            ],
        }
        out = _compact_queue(report)
        self.assertEqual(out["pending"], 2)
        self.assertEqual(out["ready_ids"], [10])
        self.assertEqual(out["unprocessed_ids"], [20])
        post = out["posts"][0]
        self.assertEqual(set(post.keys()), {"id", "title", "state", "attempts", "last_error"})
        self.assertEqual(post["id"], 10)
        self.assertEqual(post["title"], "Post A")
        self.assertEqual(post["state"], "ready")
        self.assertEqual(len(post["last_error"]), 80)  # truncado

    def test_compact_queue_truncates_last_error(self):
        report = {"posts": [{"id": 1, "title": "T", "state": "blocked", "attempts": 2, "last_error": "e" * 200}]}
        out = _compact_queue(report)
        self.assertEqual(len(out["posts"][0]["last_error"]), 80)

    def test_publish_ready_reports_retry_results_even_without_publication(self):
        from unicornio_editor import cli
        from unicornio_editor.config import Config

        outcomes = [
            {
                "post_id": 115286,
                "status": "retry",
                "wordpress_changed": False,
                "reason": "featured attachment lookup timed out",
                "retryable_errors": [{"name": "featured_identity_matches_current_attachment"}],
            },
            {"post_id": 115287, "status": "skipped", "wordpress_changed": False},
            {"post_id": 115288, "status": "error", "wordpress_changed": False, "reason": "unexpected"},
            {"post_id": 115289, "status": "blocked", "wordpress_changed": False, "reason": "quality"},
        ]
        config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with mock.patch.object(cli, "_carregar_env_do_projeto"), \
                    mock.patch.object(cli, "load_config", return_value=config), \
                    mock.patch.object(cli, "WordPressClient"), \
                    mock.patch.object(cli, "publish_ready_posts", return_value=outcomes), \
                    redirect_stdout(output):
                code = cli.main(["publish-ready", "--root", str(root)])

        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["published"], 0)
        self.assertEqual(report["retryable_count"], 1)
        self.assertEqual(report["retryable_posts"][0]["post_id"], 115286)
        self.assertEqual(
            report["retryable_posts"][0]["checks"],
            ["featured_identity_matches_current_attachment"],
        )
        self.assertEqual(report["quality_blocked"], 1)
        self.assertEqual(report["not_published"], 4)
        self.assertEqual(report["blocked_or_skipped"], 2)
        self.assertEqual(report["skipped_count"], 1)
        self.assertEqual(report["error_count"], 1)

    def test_vision_batch_does_not_write_url_only_legacy_cache(self):
        from unicornio_editor import cli
        from unicornio_editor.config import Config

        batch = {
            "schema_version": 1,
            "batch_id": "vision-cache-test",
            "items": [{
                "candidate_id": "candidate-1",
                "image_url": "https://cdn.test/image.webp",
                "subject": "Xbox Live",
                "post_id": 115286,
                "require_key_art": False,
            }],
        }
        decision = {
            "status": "MATCH", "verdict": "accept", "confidence": 0.93,
            "visual_type": "key_art", "relation": "direct",
            "editorial_fit": "appropriate", "role_fit": "inline",
            "dimensions_source": "provider",
        }
        config = Config(
            "wordpress", "http://wp.test", "/wp-json/wp/v2",
            vision_api_key="not-a-real-key", vision_base_url="https://vision.test/v1",
            vision_model="model-a",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with mock.patch.object(cli, "_carregar_env_do_projeto"), \
                    mock.patch.object(cli, "load_config", return_value=config), \
                    mock.patch.object(cli, "WordPressClient"), \
                    mock.patch.object(cli, "load_vision_batch", return_value=batch), \
                    mock.patch(
                        "unicornio_editor.media.vision_gate.verify_image_subject_batch",
                        return_value={"candidate-1": decision},
                    ), \
                    redirect_stdout(output):
                code = cli.main([
                    "vision-batch", str(root / "input.json"), "--root", str(root), "--full",
                ])

            report = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(report["cached"], 0)
            self.assertFalse((root / "work" / "vision_cache.json").exists())


if __name__ == "__main__":
    unittest.main()
