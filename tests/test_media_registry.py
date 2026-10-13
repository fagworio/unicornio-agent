import json
from types import SimpleNamespace

import pytest

from unicornio_editor.media.registry import (
    RegistryError, connect, load_v2_media, migrate_legacy_index, record_v2_observation, registry_path,
)
from unicornio_editor.pipeline_v2.model import InlineMedia, MediaProgress, FeaturedProgress, FeaturedStatus, MediaSearchProgress


def test_registry_is_per_root_and_migrates_legacy_index_idempotently(tmp_path):
    legacy = tmp_path / "work" / "media_index.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"entries": [{
        "source_url": "https://cdn.test/a.webp",
        "source_page": "https://source.test/a",
        "phash": "0000",
        "media_id": 42,
    }]}), encoding="utf-8")

    assert migrate_legacy_index(tmp_path) == {
        "scanned": 1, "inserted": 1, "updated": 0, "unchanged": 0, "skipped": 0,
    }
    assert migrate_legacy_index(tmp_path) == {
        "scanned": 1, "inserted": 0, "updated": 0, "unchanged": 1, "skipped": 0,
    }
    assert registry_path(tmp_path).is_file()
    with connect(tmp_path) as db:
        row = db.execute("SELECT canonical_url, wordpress_media_id FROM media_assets").fetchone()
        assert tuple(row) == ("https://cdn.test/a.webp", 42)


def test_registry_reconciles_legacy_metadata_and_keeps_roots_independent(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    for root in (root_a, root_b):
        path = root / "work" / "media_index.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"entries": [{"source_url": "https://cdn.test/a.webp", "subject": root.name}]}), encoding="utf-8")
    migrate_legacy_index(root_a)
    migrate_legacy_index(root_b)
    with connect(root_a) as db:
        assert db.execute("SELECT subject FROM media_assets").fetchone()[0] == "a"
    with connect(root_b) as db:
        assert db.execute("SELECT subject FROM media_assets").fetchone()[0] == "b"


def test_legacy_reconciliation_preserves_stronger_existing_evidence(tmp_path):
    with connect(tmp_path) as db:
        db.execute(
            "INSERT INTO media_assets(canonical_url, source_page_url, phash, subject, legacy_uses) VALUES (?, ?, ?, ?, ?)",
            ("https://cdn.test/a.webp", "https://trusted.test/a", "abcd", "existing", 9),
        )
        db.commit()
    legacy = tmp_path / "work" / "media_index.json"
    legacy.write_text(json.dumps({"entries": [{"source_url": "https://cdn.test/a.webp", "uses": 1}]}), encoding="utf-8")

    assert migrate_legacy_index(tmp_path)["unchanged"] == 1
    with connect(tmp_path) as db:
        row = db.execute("SELECT source_page_url, phash, subject, legacy_uses FROM media_assets").fetchone()
        assert tuple(row) == ("https://trusted.test/a", "abcd", "existing", 9)


def test_legacy_report_counts_invalid_entries_as_skipped(tmp_path):
    legacy = tmp_path / "work" / "media_index.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"entries": [None, {}, {"source_url": ""}]}), encoding="utf-8")

    assert migrate_legacy_index(tmp_path) == {
        "scanned": 2, "inserted": 0, "updated": 0, "unchanged": 0, "skipped": 3,
    }


def test_registry_rejects_future_schema_without_downgrade(tmp_path):
    with connect(tmp_path) as db:
        db.execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
        db.commit()
    with pytest.raises(RegistryError):
        connect(tmp_path)


def test_registry_schema_has_decisions_usage_and_runs(tmp_path):
    with connect(tmp_path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        db.execute("INSERT INTO media_assets(canonical_url) VALUES('https://cdn.test/a')")
        asset_id = db.execute("SELECT asset_id FROM media_assets").fetchone()[0]
        db.execute("INSERT INTO media_usage(post_id, asset_id, role) VALUES(1, ?, 'inline')", (asset_id,))
        with pytest.raises(Exception):
            db.execute("INSERT INTO media_usage(post_id, asset_id, role) VALUES(1, ?, 'inline')", (asset_id,))
    assert {"media_assets", "media_decisions", "media_usage", "media_runs", "registry_migration_log"} <= tables


def test_registry_migrates_true_v1_usage_table_and_consolidates_duplicates(tmp_path):
    path = registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    import sqlite3
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES ('schema_version', '1');
        CREATE TABLE media_assets (asset_id INTEGER PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE);
        INSERT INTO media_assets(asset_id, canonical_url) VALUES (2, 'https://cdn.test/a');
        CREATE TABLE media_usage (
            post_id INTEGER NOT NULL, asset_id INTEGER NOT NULL, wordpress_media_id INTEGER,
            role TEXT NOT NULL, list_item TEXT NOT NULL DEFAULT '', section_slot INTEGER,
            status TEXT NOT NULL DEFAULT 'accepted', applied_at TEXT
        );
        INSERT INTO media_usage(post_id, asset_id, role) VALUES (1, 2, 'inline');
        INSERT INTO media_usage(post_id, asset_id, role) VALUES (1, 2, 'inline');
        CREATE INDEX uq_media_usage_position
            ON media_usage(post_id, asset_id, role, COALESCE(section_slot, -1));
    """)
    db.commit()
    db.close()

    with connect(tmp_path) as migrated:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(media_usage)")}
        assert "usage_id" in columns
        assert migrated.execute("SELECT COUNT(*) FROM media_usage").fetchone()[0] == 1
        assert migrated.execute("SELECT COUNT(*) FROM registry_migration_log").fetchone()[0] == 1
        assert migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "2"
        migrated.execute("INSERT INTO media_assets(asset_id, canonical_url) VALUES (3, 'https://cdn.test/b')")
        migrated.execute("INSERT INTO media_usage(post_id, asset_id, role) VALUES (1, 3, 'inline')")
        with pytest.raises(Exception):
            migrated.execute("INSERT INTO media_usage(post_id, asset_id, role) VALUES (1, 3, 'inline')")


def test_v2_observation_separates_state_readback_from_media_application(tmp_path):
    media = MediaProgress(
        required=2,
        inline=(InlineMedia(7, "https://cdn.test/inline", 0, "alt", "credit", "subject"),),
        featured=FeaturedProgress(FeaturedStatus.VALID, 8, "https://cdn.test/featured"),
        search=MediaSearchProgress(queries_attempted=2),
    )
    state = SimpleNamespace(media=media)
    record_v2_observation(tmp_path, post_id=10, run_id="run-1", state=state, phase="media", outcome="retry")
    with connect(tmp_path) as db:
        assert db.execute("SELECT status FROM media_usage WHERE post_id=10 AND role='inline'").fetchone()[0] == "accepted"
        metrics = json.loads(db.execute("SELECT planned_deficits FROM media_runs WHERE run_id='run-1'").fetchone()[0])
        assert metrics["initial"]["missing"] == 1
        assert metrics["initial"]["queries_attempted"] == 2
    assert load_v2_media(tmp_path, post_id=10) == []

    record_v2_observation(tmp_path, post_id=10, run_id="run-1", state=state, phase="validate", outcome="ready", applied=True)
    record_v2_observation(tmp_path, post_id=10, run_id="run-2", state=state, phase="media", outcome="started", applied=False)
    with connect(tmp_path) as db:
        assert db.execute("SELECT status FROM media_usage WHERE post_id=10 AND role='inline'").fetchone()[0] == "applied"
        metrics = json.loads(db.execute("SELECT planned_deficits FROM media_runs WHERE run_id='run-1'").fetchone()[0])
        assert metrics["final"]["phase"] == "validate"
    reused = load_v2_media(tmp_path, post_id=10)
    assert {row["role"] for row in reused} == {"inline", "featured"}
    assert all(row["status"] == "applied" for row in reused)
