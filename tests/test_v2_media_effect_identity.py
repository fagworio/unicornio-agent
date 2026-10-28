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
