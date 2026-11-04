from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from unicornio_editor.cli import main
from unicornio_editor.media.funnel_report import media_funnel_report
from unicornio_editor.media.registry import connect, registry_path


def test_media_funnel_report_aggregates_failures_without_writing_or_leaking_evidence(tmp_path):
    with connect(tmp_path) as db:
        db.execute(
            """INSERT INTO media_runs
            (run_id, post_id, queries_attempted, acquisition_outcome, completion_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            ("run-incomplete", 115975, 3, "provider_error", "SEARCH_INCOMPLETE", "2026-11-03T10:00:00+00:00"),
        )
        db.execute(
            """INSERT INTO media_runs
            (run_id, post_id, planned_deficits, queries_attempted, acquisition_outcome, completion_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "run-complete", 115976,
                '{"final":{"phase":"MEDIA","search_completion_reason":"TARGET_REACHED"}}',
                2, "target_reached", "TARGET_REACHED", "2026-11-03T11:00:00+00:00",
            ),
        )
        db.execute(
            """INSERT INTO media_decisions
            (post_id, run_id, candidate_id, role, reason_code, disposition, evidence_reference, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (115975, "run-incomplete", "candidate-1", "inline", "source_mismatch", "reject_final", '{"signed_url":"SECRET"}', "2026-11-03T10:01:00+00:00"),
        )
        db.execute(
            """INSERT INTO media_decisions
            (post_id, run_id, candidate_id, role, reason_code, disposition, evidence_reference, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (115975, "run-incomplete", "candidate-2", "inline", "unresolved_source", "defer_retry", "private evidence", "2026-11-03T10:02:00+00:00"),
        )
        db.execute(
            """INSERT INTO media_decisions
            (post_id, run_id, candidate_id, role, reason_code, disposition, evidence_reference, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (115976, "run-complete", "candidate-3", "featured", "reject", "reject_final", "private evidence", "2026-11-03T11:01:00+00:00"),
        )
        db.execute(
            """INSERT INTO media_decisions
            (post_id, run_id, candidate_id, role, reason_code, disposition, evidence_reference, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (115975, "run-incomplete", "candidate-4", "inline", "candidate_rejected", "reject_final", '{"decision_causes":["source_mismatch"],"direct_image_url":"SECRET"}', "2026-11-03T10:03:00+00:00"),
        )

    path = registry_path(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = media_funnel_report(
        tmp_path,
        since_days=7,
        now=datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
    )
    after = hashlib.sha256(path.read_bytes()).hexdigest()

    assert result["status"] == "ok"
    assert result["read_only"] is True
    assert result["metrics"]["media_runs"] == 2
    assert result["metrics"]["queries_attempted"] == 5
    assert result["metrics"]["search_incomplete_runs"] == 1
    assert result["metrics"]["media_phase_runs"] == 1
    assert result["metrics"]["search_completion_reasons"] == {
        "SEARCH_INCOMPLETE": 1,
        "TARGET_REACHED": 1,
    }
    assert result["metrics"]["source_mismatch"] == 2
    assert result["metrics"]["unresolved_source"] == 1
    assert result["metrics"]["featured_rejections"] == 1
    assert result["posts"] == [
        {
            "post_id": 115975,
            "search_incomplete_runs": 1,
            "source_mismatch": 2,
            "unresolved_source": 1,
        },
        {"post_id": 115976, "featured_rejections": 1},
    ]
    assert "SECRET" not in str(result)
    assert "signed_url" not in str(result)
    assert before == after


def test_media_funnel_report_excludes_records_outside_window_and_handles_missing_registry(tmp_path):
    missing = media_funnel_report(tmp_path)
    assert missing["status"] == "unavailable"
    assert missing["reason"] == "media_registry_missing"

    with connect(tmp_path) as db:
        db.execute(
            """INSERT INTO media_runs
            (run_id, post_id, queries_attempted, acquisition_outcome, completion_reason, created_at)
            VALUES ('old', 1, 99, 'provider_error', 'SEARCH_INCOMPLETE', '2025-01-01T00:00:00+00:00')"""
        )
    result = media_funnel_report(
        tmp_path,
        since_days=1,
        now=datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
    )
    assert result["status"] == "ok"
    assert result["metrics"]["media_runs"] == 0
    assert result["metrics"]["queries_attempted"] == 0


def test_media_funnel_cli_is_local_read_only_and_does_not_require_wordpress_config(tmp_path, capsys):
    exit_code = main(["v2-media-funnel-report", "--root", str(tmp_path)])
    output = capsys.readouterr().out
    assert exit_code == 2
    assert '"status": "unavailable"' in output
    assert '"reason": "media_registry_missing"' in output
