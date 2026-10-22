import json
import os
import sqlite3
import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from hermes import relatorio_publicacao as report
from hermes import cost_guard
from hermes.cost_guard import cost_in_last_24h, cost_measurement_in_last_24h


class PublicationReportCostTests(unittest.TestCase):
    def _database(self, *, with_job_id: bool, with_project: bool = False) -> Path:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        path = Path(handle.name)
        db = sqlite3.connect(path)
        job_column = ", job_id TEXT" if with_job_id else ""
        project_columns = ", cwd TEXT, git_repo_root TEXT" if with_project else ""
        db.execute(
            "CREATE TABLE sessions (source TEXT, started_at INTEGER, "
            f"estimated_cost_usd REAL{job_column}{project_columns})"
        )
        if with_job_id:
            rows = [
                ("cron", 9_999_999_999, 0.20, "editorial"),
                ("cron", 9_999_999_999, 0.80, "other"),
            ]
            db.executemany(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                rows,
            )
        elif with_project:
            db.executemany(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                [
                    ("cron", 9_999_999_999, 0.20, "/project", "/project"),
                    ("cron", 9_999_999_999, 0.80, "/other", "/other"),
                ],
            )
        else:
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                ("cron", 9_999_999_999, 0.20),
            )
        db.commit()
        db.close()
        self.addCleanup(path.unlink)
        return path

    def test_cost_is_scoped_to_editorial_job_when_schema_supports_it(self):
        path = self._database(with_job_id=True)
        with patch.object(report, "STATE_DB", path), patch.dict(
            os.environ, {"HERMES_EDITORIAL_CRON_JOB_ID": "editorial"}, clear=False
        ):
            cost, runs, scope = report._custo_24h()
        self.assertEqual((cost, runs), (0.20, 1))
        self.assertEqual(scope, "job editorial editorial")

    def test_cost_uses_project_scope_when_schema_has_no_job(self):
        path = self._database(with_job_id=False, with_project=True)
        with patch.object(report, "STATE_DB", path), patch.dict(
            os.environ, {"HERMES_EDITORIAL_CRON_JOB_ID": ""}, clear=False
        ):
            with patch.object(report, "ROOT", Path("/project")):
                cost, runs, scope = report._custo_24h()
        self.assertEqual((cost, runs), (0.20, 1))
        self.assertEqual(scope, "projeto editorial (cwd/git_repo_root)")

    def test_cost_requires_job_id_when_schema_supports_it(self):
        measured = cost_measurement_in_last_24h(
            self._database(with_job_id=True), project_root="/project"
        )
        self.assertIsNone(measured)

    def test_cost_guard_requires_exact_job_attribution(self):
        self.assertIsNone(cost_in_last_24h(self._database(with_job_id=False), "editorial"))
        self.assertEqual(cost_in_last_24h(self._database(with_job_id=True), "editorial"), (0.20, 1))

    def test_cost_guard_uses_project_attribution_without_job_column(self):
        measured = cost_measurement_in_last_24h(
            self._database(with_job_id=False, with_project=True), project_root="/project"
        )
        self.assertEqual(measured, (0.20, 1, "projeto editorial (cwd/git_repo_root)"))

    def test_null_cwd_project_fallback_is_attribution_unavailable(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        path = Path(handle.name)
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE sessions (source TEXT, started_at INTEGER, "
            "estimated_cost_usd REAL, cwd TEXT, git_repo_root TEXT)"
        )
        db.execute(
            "INSERT INTO sessions VALUES ('cron', ?, 0.80, NULL, NULL)",
            (int(time.time()),),
        )
        db.commit()
        db.close()
        self.addCleanup(path.unlink)

        self.assertIsNone(
            cost_measurement_in_last_24h(path, project_root="/project")
        )
        output = StringIO()
        with patch("sys.argv", [
            "cost_guard.py", "--state-db", str(path),
            "--project-root", "/project", "--limit", "1",
        ]), patch("sys.stdout", output):
            self.assertEqual(cost_guard.main(), 0)
        self.assertEqual(
            json.loads(output.getvalue())["reason"],
            "attribution_unavailable",
        )

    def test_report_distinguishes_pending_from_posts_ready_to_publish(self):
        data = {
            "posts": [],
            "blocked_posts": [],
        }
        with patch.object(report, "_load_window_json", return_value=data), patch.object(
            report, "_custo_24h", return_value=(0.0, 0, "test")
        ), patch.object(report, "_published_by_editorial_last_24h", return_value=0), patch.object(
            report,
            "_queue_after_window",
            return_value={
                "pending": 5,
                "edited": 0,
                "blocked": 0,
                "uncertain": 5,
                "awaiting_human": 0,
                "skipped": 0,
            },
        ), patch("sys.stdout", new_callable=StringIO) as output:
            self.assertEqual(report.main(), 0)

        text = output.getvalue()
        self.assertIn("5 pending no WP | 0 pronta(s) para a próxima janela", text)
        self.assertIn("5 em revisão humana", text)

    def test_report_does_not_present_unknown_cost_as_zero(self):
        with patch.object(report, "_load_window_json", return_value={"posts": [], "blocked_posts": []}), patch.object(
            report, "_custo_24h", return_value=None
        ), patch.object(report, "_published_by_editorial_last_24h", return_value=0), patch.object(
            report, "_queue_after_window", return_value=None
        ), patch("sys.stdout", new_callable=StringIO) as output:
            self.assertEqual(report.main(), 0)

        self.assertIn("Custo editorial (24h): indisponível", output.getvalue())

    def test_report_shows_retry_only_window_with_reason_and_checks(self):
        data = {
            "posts": [],
            "blocked_posts": [],
            "retryable_count": 1,
            "retryable_posts": [{
                "post_id": 115286,
                "reason": "validação técnica da mídia inconclusiva; publicação não realizada",
                "checks": ["featured_identity_matches_current_attachment"],
            }],
            "error_count": 0,
            "error_posts": [],
        }
        with patch.object(report, "_load_window_json", return_value=data), patch.object(
            report, "_post_titles", return_value={115286: "Post de teste"}
        ), patch.object(report, "_custo_24h", return_value=(0.0, 0, "test")), patch.object(
            report, "_published_by_editorial_last_24h", return_value=0
        ), patch.object(report, "_queue_after_window", return_value=None), patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            self.assertEqual(report.main(), 0)

        text = output.getvalue()
        self.assertIn("Publicados novos: 0", text)
        self.assertIn("Publicados reconciliados: 0", text)
        self.assertIn("Retries técnicos (1)", text)
        self.assertIn("115286 | Post de teste | retry", text)
        self.assertIn("featured_identity_matches_current_attachment", text)
        self.assertIn("Erros inesperados: 0", text)

    def test_report_separates_new_and_reconciled_publications(self):
        data = {
            "posts": [
                {"post_id": 115290, "publication_kind": "reconciled", "published_at": "2026-10-16T12:00:00Z"},
                {"post_id": 115234, "publication_kind": "new", "published_at": "2026-10-16T12:01:00Z"},
            ],
            "blocked_posts": [],
            "retryable_posts": [],
            "error_posts": [],
        }
        with patch.object(report, "_load_window_json", return_value=data), patch.object(
            report, "_post_titles", return_value={115290: "Reconciliado", 115234: "Novo"}
        ), patch.object(report, "_custo_24h", return_value=(0.0, 0, "test")), patch.object(
            report, "_published_by_editorial_last_24h", return_value=1
        ), patch.object(report, "_queue_after_window", return_value=None), patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            self.assertEqual(report.main(), 0)

        text = output.getvalue()
        self.assertIn("Publicados novos (1)", text)
        self.assertIn("Publicados reconciliados (1)", text)
        self.assertIn("115234 | Novo", text)
        self.assertIn("115290 | Reconciliado", text)

    def test_report_redacts_secrets_in_retry_and_error_details(self):
        self.assertNotIn("pw123", report._safe_reason("password=pw123"))
        self.assertNotIn("abc123", report._safe_reason("Bearer abc123"))

    def test_blocked_fallback_reason_is_sanitized_in_telegram_report(self):
        data = {
            "posts": [],
            "blocked_posts": [{
                "post_id": 71,
                "reason": "authorization=private-value",
            }],
            "retryable_posts": [],
            "error_posts": [],
        }
        with patch.object(report, "_load_window_json", return_value=data), patch.object(
            report, "_post_titles", return_value={71: "Bloqueado"}
        ), patch.object(report, "_custo_24h", return_value=(0.0, 0, "test")), patch.object(
            report, "_published_by_editorial_last_24h", return_value=0
        ), patch.object(report, "_queue_after_window", return_value=None), patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            self.assertEqual(report.main(), 0)

        self.assertNotIn("private-value", output.getvalue())
        self.assertIn("[redacted", output.getvalue())


if __name__ == "__main__":
    unittest.main()
