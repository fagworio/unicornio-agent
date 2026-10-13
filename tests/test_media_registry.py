import json

import pytest

from unicornio_editor.media.registry import RegistryError, connect, migrate_legacy_index, registry_path


def test_registry_is_per_root_and_migrates_legacy_index_idempotently(tmp_path):
    legacy = tmp_path / "work" / "media_index.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"entries": [{
        "source_url": "https://cdn.test/a.webp",
        "source_page": "https://source.test/a",
        "phash": "0000",
        "media_id": 42,
    }]}), encoding="utf-8")

    assert migrate_legacy_index(tmp_path) == 1
    assert migrate_legacy_index(tmp_path) == 1  # reconciliação é idempotente, não duplicadora
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
    assert {"media_assets", "media_decisions", "media_usage", "media_runs"} <= tables
