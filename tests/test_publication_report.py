import os
import sqlite3
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from hermes import relatorio_publicacao as report
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

    def test_cost_guard_requires_exact_job_attribution(self):
        self.assertIsNone(cost_in_last_24h(self._database(with_job_id=False), "editorial"))
        self.assertEqual(cost_in_last_24h(self._database(with_job_id=True), "editorial"), (0.20, 1))

    def test_cost_guard_uses_project_attribution_without_job_column(self):
        measured = cost_measurement_in_last_24h(
            self._database(with_job_id=False, with_project=True), project_root="/project"
        )
        self.assertEqual(measured, (0.20, 1, "projeto editorial (cwd/git_repo_root)"))

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


if __name__ == "__main__":
    unittest.main()
