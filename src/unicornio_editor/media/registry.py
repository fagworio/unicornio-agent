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

SCHEMA_VERSION = 2


class RegistryError(RuntimeError):
    """Raised when a registry cannot be safely opened or migrated."""


class _RegistryConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def registry_path(root: str | Path) -> Path:
    return Path(root) / "work" / "media_registry.sqlite"


def connect(root: str | Path) -> sqlite3.Connection:
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, factory=_RegistryConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA journal_mode = WAL")
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    current = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if current:
        version_row = db.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        version = int(version_row[0]) if version_row else 0
        if version > SCHEMA_VERSION:
            raise RegistryError(f"schema de registry futuro nao suportado: {version}")
    else:
        version = 0
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
            subject TEXT NOT NULL DEFAULT '',
            article_id INTEGER,
            legacy_uses INTEGER NOT NULL DEFAULT 0,
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
            usage_id INTEGER PRIMARY KEY
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
        CREATE UNIQUE INDEX IF NOT EXISTS uq_media_usage_position
            ON media_usage(post_id, asset_id, role, COALESCE(section_slot, -1));
        """
    )
    if version < 2:
        columns = {row[1] for row in db.execute("PRAGMA table_info(media_assets)")}
        if "subject" not in columns:
            db.execute("ALTER TABLE media_assets ADD COLUMN subject TEXT NOT NULL DEFAULT ''")
        if "article_id" not in columns:
            db.execute("ALTER TABLE media_assets ADD COLUMN article_id INTEGER")
        if "legacy_uses" not in columns:
            db.execute("ALTER TABLE media_assets ADD COLUMN legacy_uses INTEGER NOT NULL DEFAULT 0")
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
            db.execute(
                """INSERT INTO media_assets
                (canonical_url, source_page_url, source_type, sha256, phash,
                 visual_group_id, wordpress_media_id, subject, article_id,
                 legacy_uses, status)
                VALUES (?, ?, 'legacy_index', ?, ?, ?, ?, ?, ?, ?, 'known')
                ON CONFLICT(canonical_url) DO UPDATE SET
                    source_page_url=excluded.source_page_url,
                    sha256=CASE WHEN excluded.sha256 <> '' THEN excluded.sha256 ELSE media_assets.sha256 END,
                    phash=CASE WHEN excluded.phash <> '' THEN excluded.phash ELSE media_assets.phash END,
                    visual_group_id=CASE WHEN excluded.visual_group_id <> '' THEN excluded.visual_group_id ELSE media_assets.visual_group_id END,
                    wordpress_media_id=COALESCE(excluded.wordpress_media_id, media_assets.wordpress_media_id),
                    subject=CASE WHEN excluded.subject <> '' THEN excluded.subject ELSE media_assets.subject END,
                    article_id=COALESCE(excluded.article_id, media_assets.article_id),
                    legacy_uses=MAX(excluded.legacy_uses, media_assets.legacy_uses)""",
                (url, str(entry.get("source_page") or ""), str(entry.get("sha256") or ""),
                 str(entry.get("phash") or ""), str(entry.get("visual_group_id") or ""),
                 entry.get("media_id"), str(entry.get("subject") or ""),
                 entry.get("article_id"), int(entry.get("uses") or 0)),
            )
            inserted += 1
        db.commit()
    return inserted


__all__ = ["SCHEMA_VERSION", "connect", "migrate_legacy_index", "registry_path"]
