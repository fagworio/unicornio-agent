"""Testes dos modos compactos do CLI (economia de tokens).

As projecoes compactas seguem a regra: SUCCESS = minimo de informacao;
FAILURE = somente o necessario para corrigir. O relatorio completo fica em
arquivo (apply.latest.json) para auditoria.
"""

import unittest

from unicornio_editor.cli import _compact_apply, _compact_checklist, _monitor_line, _record_cmd_output


class CompactOutputTests(unittest.TestCase):
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


    def test_record_cmd_output_only_for_context_commands(self):
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
            # Comando de escrita nao gera cmd_output.
            args.command = "apply"
            _record_cmd_output(args, {"status": "ready"})
            from unicornio_editor.observability import read_telemetry_summary

            summary = read_telemetry_summary(root)
            self.assertEqual(summary["by_event"].get("cmd_output"), 1)
            self.assertGreater(summary["context_bytes_total"], 0)
            self.assertIn("cards", summary["context_bytes_by_command"])


if __name__ == "__main__":
    unittest.main()
