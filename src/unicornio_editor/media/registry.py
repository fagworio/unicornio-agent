"""Per-installation SQLite registry for V2 media evidence and decisions.

The registry is intentionally local to ``root``. It stores operational
evidence, never WordPress lifecycle state, and can be rebuilt idempotently from
the legacy media index without copying a production database into another
environment.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def registry_path(root: str | Path) -> Path:
    return Path(root) / "work" / "media_registry.sqlite"


def connect(root: str | Path) -> sqlite3.Connection:
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS media_assets (
            asset_id INTEGER PRIMARY KEY,
            canonical_url TEXT NOT NULL UNIQUE,
            source_page_url TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL DEFAULT '',
            phash TEXT NOT NULL DEFAULT '',
            visual_group_id TEXT NOT NULL DEFAULT '',
            width INTEGER,
            height INTEGER,
            mime_type TEXT NOT NULL DEFAULT '',
            wordpress_media_id INTEGER,
            provenance_evidence TEXT NOT NULL DEFAULT '{}',
            identity_evidence TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'known',
            policy_version TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS media_decisions (
            decision_id INTEGER PRIMARY KEY,
            post_id INTEGER NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            candidate_id TEXT NOT NULL DEFAULT '',
            asset_id INTEGER REFERENCES media_assets(asset_id),
            role TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            reason_code TEXT NOT NULL,
            disposition TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'post',
            retry_after TEXT,
            evidence_reference TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS media_usage (
            post_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL REFERENCES media_assets(asset_id),
            wordpress_media_id INTEGER,
            role TEXT NOT NULL,
            list_item TEXT NOT NULL DEFAULT '',
            section_slot INTEGER,
            status TEXT NOT NULL DEFAULT 'accepted',
            applied_at TEXT,
            PRIMARY KEY (post_id, asset_id, role, section_slot)
        );
        CREATE TABLE IF NOT EXISTS media_runs (
            run_id TEXT PRIMARY KEY,
            post_id INTEGER NOT NULL,
            planned_deficits TEXT NOT NULL DEFAULT '{}',
            queries_attempted INTEGER NOT NULL DEFAULT 0,
            acquisition_outcome TEXT NOT NULL DEFAULT '',
            checkpoint_reference TEXT NOT NULL DEFAULT '',
            completion_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_media_decisions_post ON media_decisions(post_id);
        CREATE INDEX IF NOT EXISTS idx_media_decisions_candidate ON media_decisions(candidate_id);
        CREATE INDEX IF NOT EXISTS idx_media_assets_phash ON media_assets(phash);
        CREATE INDEX IF NOT EXISTS idx_media_assets_wp ON media_assets(wordpress_media_id);
        """
    )
    db.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    db.commit()


def migrate_legacy_index(root: str | Path) -> int:
    """Import ``work/media_index.json`` idempotently; return inserted count."""
    legacy = Path(root) / "work" / "media_index.json"
    if not legacy.is_file():
        return 0
    try:
        payload = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return 0
    inserted = 0
    with connect(root) as db:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("source_url") or "").strip()
            if not url:
                continue
            before = db.total_changes
            db.execute(
                """INSERT OR IGNORE INTO media_assets
                (canonical_url, source_page_url, source_type, sha256, phash,
                 visual_group_id, wordpress_media_id, status)
                VALUES (?, ?, 'legacy_index', ?, ?, ?, ?, 'known')""",
                (url, str(entry.get("source_page") or ""), str(entry.get("sha256") or ""),
                 str(entry.get("phash") or ""), str(entry.get("visual_group_id") or ""),
                 entry.get("media_id")),
            )
            inserted += int(db.total_changes > before)
        db.commit()
    return inserted


__all__ = ["SCHEMA_VERSION", "connect", "migrate_legacy_index", "registry_path"]
