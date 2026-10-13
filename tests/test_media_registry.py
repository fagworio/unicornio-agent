import json

from unicornio_editor.media.registry import connect, migrate_legacy_index, registry_path


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
    assert migrate_legacy_index(tmp_path) == 0
    assert registry_path(tmp_path).is_file()
    with connect(tmp_path) as db:
        row = db.execute("SELECT canonical_url, wordpress_media_id FROM media_assets").fetchone()
        assert tuple(row) == ("https://cdn.test/a.webp", 42)


def test_registry_schema_has_decisions_usage_and_runs(tmp_path):
    with connect(tmp_path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"media_assets", "media_decisions", "media_usage", "media_runs"} <= tables
