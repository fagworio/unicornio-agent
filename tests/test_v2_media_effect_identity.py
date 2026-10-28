import sqlite3

from unicornio_editor.media.registry import connect
from unicornio_editor.pipeline_v2.runtime import _media_effect_evidence


def _journal():
    return {
        "post_id": 115736,
        "run_id": "current-run",
        "cycle_id": "current-cycle",
        "cycle_round": 1,
        "authorization_id": "current-auth",
        "state": {"media": {"inline": {"accepted": []}, "featured": None}},
    }


def _insert_usage(root, *, media_id, run_id, cycle_id, cycle_round, authorization_id, status):
    with connect(root) as db:
        db.execute(
            """INSERT INTO media_assets
               (canonical_url, source_type, sha256, phash, visual_group_id,
                wordpress_media_id, subject, status)
               VALUES (?, 'test', '', '', '', ?, '', 'known')""",
            (f"https://example.test/{media_id}.webp", media_id),
        )
        asset_id = db.execute(
            "SELECT asset_id FROM media_assets WHERE wordpress_media_id = ?", (media_id,)
        ).fetchone()[0]
        db.execute(
            """INSERT INTO media_usage
               (post_id, asset_id, wordpress_media_id, role, status, run_id,
                cycle_id, cycle_round, authorization_id)
               VALUES (?, ?, ?, 'inline', ?, ?, ?, ?, ?)""",
            (115736, asset_id, media_id, status, run_id, cycle_id, cycle_round, authorization_id),
        )
        db.commit()


def _update_usage(root, media_id, **fields):
    allowed = {"accepted_at", "applied_at", "readback_confirmed_at"}
    assert set(fields) <= allowed
    with connect(root) as db:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        db.execute(
            f"UPDATE media_usage SET {assignments} WHERE wordpress_media_id = ?",
            (*fields.values(), media_id),
        )
        db.commit()


def test_historical_media_usage_is_ignored_but_current_applied_usage_is_effect(tmp_path):
    journal = _journal()
    _insert_usage(
        tmp_path,
        media_id=115863,
        run_id="old-run",
        cycle_id="old-cycle",
        cycle_round=9,
        authorization_id="old-auth",
        status="applied",
    )
    _update_usage(tmp_path, 115863, applied_at="2026-10-27T12:00:00+00:00")

    historical = _media_effect_evidence(tmp_path, 115736, journal)
    assert historical["effects_detected"] is False
    assert historical["applied_media_ids"] == []
    assert historical["historical_media_usage_ignored"][0]["media_id"] == 115863

    _insert_usage(
        tmp_path,
        media_id=115864,
        run_id="current-run",
        cycle_id="current-cycle",
        cycle_round=1,
        authorization_id="current-auth",
        status="applied",
    )
    current = _media_effect_evidence(tmp_path, 115736, journal)
    assert current["effects_detected"] is True
    assert current["applied_media_ids"] == [115864]
    assert any(item["media_id"] == 115863 for item in current["historical_media_usage_ignored"])


def test_incomplete_identity_is_uncertain_and_blocks_absence_recovery(tmp_path):
    journal = _journal()
    _insert_usage(
        tmp_path,
        media_id=115865,
        run_id="old-run",
        cycle_id="",
        cycle_round=None,
        authorization_id="old-auth",
        status="applied",
    )
    result = _media_effect_evidence(tmp_path, 115736, journal)
    row = next(item for item in result["media_usage_classifications"] if item["media_id"] == 115865)
    assert row["classification"] == "IDENTITY_UNCERTAIN"
    assert result["effects_detected"] is True
    assert result["historical_media_usage_ignored"] == []


def test_ambiguous_timestamp_is_uncertain_even_with_foreign_identity(tmp_path):
    journal = _journal()
    _insert_usage(
        tmp_path,
        media_id=115866,
        run_id="old-run",
        cycle_id="old-cycle",
        cycle_round=9,
        authorization_id="old-auth",
        status="applied",
    )
    _update_usage(tmp_path, 115866, applied_at="2026-10-28 12:00:00")
    result = _media_effect_evidence(tmp_path, 115736, journal)
    row = next(item for item in result["media_usage_classifications"] if item["media_id"] == 115866)
    assert row["classification"] == "IDENTITY_UNCERTAIN"
    assert row["timestamps"] == ["missing", "ambiguous", "missing"]
    assert result["effects_detected"] is True
