import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes import relatorio_publicacao as report
from hermes.cost_guard import cost_in_last_24h


class PublicationReportCostTests(unittest.TestCase):
    def _database(self, *, with_job_id: bool) -> Path:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        path = Path(handle.name)
        db = sqlite3.connect(path)
        job_column = ", job_id TEXT" if with_job_id else ""
        db.execute(
            "CREATE TABLE sessions (source TEXT, started_at INTEGER, "
            f"estimated_cost_usd REAL{job_column})"
        )
        if with_job_id:
            db.executemany(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                [
                    ("cron", 9_999_999_999, 0.20, "editorial"),
                    ("cron", 9_999_999_999, 0.80, "other"),
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

    def test_cost_declares_aggregate_scope_when_schema_has_no_job(self):
        path = self._database(with_job_id=False)
        with patch.object(report, "STATE_DB", path), patch.dict(
            os.environ, {"HERMES_EDITORIAL_CRON_JOB_ID": "editorial"}, clear=False
        ):
            cost, runs, scope = report._custo_24h()
        self.assertEqual((cost, runs), (0.20, 1))
        self.assertEqual(scope, "todos os crons (state.db sem coluna de job)")

    def test_cost_guard_requires_exact_job_attribution(self):
        self.assertIsNone(cost_in_last_24h(self._database(with_job_id=False), "editorial"))
        self.assertEqual(cost_in_last_24h(self._database(with_job_id=True), "editorial"), (0.20, 1))


if __name__ == "__main__":
    unittest.main()
