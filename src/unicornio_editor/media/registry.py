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
    db.execute("BEGIN")
    try:
        usage_columns = {
            row[1] for row in db.execute("PRAGMA table_info(media_usage)")
        } if current else set()
        if usage_columns and "usage_id" not in usage_columns:
            db.execute("ALTER TABLE media_usage RENAME TO media_usage_v1")
        schema_sql = """
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
        CREATE TABLE IF NOT EXISTS registry_migration_log (
            migration_id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_media_decisions_post ON media_decisions(post_id);
        CREATE INDEX IF NOT EXISTS idx_media_decisions_candidate ON media_decisions(candidate_id);
        """
        for statement in schema_sql.split(";"):
            statement = statement.strip()
            if statement:
                db.execute(statement)
        if usage_columns and "usage_id" not in usage_columns:
            old_rows = db.execute(
                "SELECT post_id, asset_id, wordpress_media_id, role, list_item, section_slot, status, applied_at "
                "FROM media_usage_v1 ORDER BY rowid"
            ).fetchall()
            seen: set[tuple[Any, ...]] = set()
            for row in old_rows:
                key = (row[0], row[1], row[3], row[5] if row[5] is not None else -1)
                if key in seen:
                    db.execute(
                        "INSERT INTO registry_migration_log(kind, detail) VALUES('usage_consolidated', ?)",
                        (json.dumps(dict(row), default=str),),
                    )
                    continue
                seen.add(key)
                db.execute(
                    """INSERT INTO media_usage
                    (post_id, asset_id, wordpress_media_id, role, list_item, section_slot, status, applied_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", tuple(row)
                )
            db.execute("DROP TABLE media_usage_v1")
        # The legacy table may have carried an index with this name.  SQLite
        # binds index names globally, so recreating it must happen only after
        # the legacy table (and its indexes) has been removed.
        db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_media_usage_position
            ON media_usage(post_id, asset_id, role, COALESCE(section_slot, -1))"""
        )
        index_columns = {
            row[2] for row in db.execute("PRAGMA index_info(uq_media_usage_position)")
        }
        if not index_columns:
            raise RegistryError("indice uq_media_usage_position nao foi criado")
        if version < 2:
            columns = {row[1] for row in db.execute("PRAGMA table_info(media_assets)")}
            additions = {
                "source_page_url": "TEXT NOT NULL DEFAULT ''",
                "source_type": "TEXT NOT NULL DEFAULT ''",
                "sha256": "TEXT NOT NULL DEFAULT ''",
                "phash": "TEXT NOT NULL DEFAULT ''",
                "visual_group_id": "TEXT NOT NULL DEFAULT ''",
                "width": "INTEGER",
                "height": "INTEGER",
                "mime_type": "TEXT NOT NULL DEFAULT ''",
                "wordpress_media_id": "INTEGER",
                "subject": "TEXT NOT NULL DEFAULT ''",
                "article_id": "INTEGER",
                "legacy_uses": "INTEGER NOT NULL DEFAULT 0",
                "provenance_evidence": "TEXT NOT NULL DEFAULT '{}'",
                "identity_evidence": "TEXT NOT NULL DEFAULT '{}'",
                "status": "TEXT NOT NULL DEFAULT 'known'",
                "policy_version": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE media_assets ADD COLUMN {name} {definition}")
        db.execute("CREATE INDEX IF NOT EXISTS idx_media_assets_phash ON media_assets(phash)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_media_assets_wp ON media_assets(wordpress_media_id)")
        db.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


def migrate_legacy_index(root: str | Path) -> dict[str, int]:
    """Reconcile ``work/media_index.json`` without replacing better evidence."""
    legacy = Path(root) / "work" / "media_index.json"
    if not legacy.is_file():
        return {"scanned": 0, "inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    try:
        payload = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"scanned": 0, "inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {"scanned": 0, "inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    report = {"scanned": 0, "inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    with connect(root) as db:
        for entry in entries:
            if not isinstance(entry, dict):
                report["skipped"] += 1
                continue
            report["scanned"] += 1
            url = str(entry.get("source_url") or "").strip()
            if not url:
                report["skipped"] += 1
                continue
            before = db.execute("SELECT * FROM media_assets WHERE canonical_url=?", (url,)).fetchone()
            db.execute(
                """INSERT INTO media_assets
                (canonical_url, source_page_url, source_type, sha256, phash,
                 visual_group_id, wordpress_media_id, subject, article_id,
                 legacy_uses, status)
                VALUES (?, ?, 'legacy_index', ?, ?, ?, ?, ?, ?, ?, 'known')
                ON CONFLICT(canonical_url) DO UPDATE SET
                    source_page_url=CASE WHEN excluded.source_page_url <> '' THEN excluded.source_page_url ELSE media_assets.source_page_url END,
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
            after = db.execute("SELECT * FROM media_assets WHERE canonical_url=?", (url,)).fetchone()
            if before is None:
                report["inserted"] += 1
            elif tuple(before) == tuple(after):
                report["unchanged"] += 1
            else:
                report["updated"] += 1
        db.commit()
    return report


def record_v2_observation(
    root: str | Path,
    *,
    post_id: int,
    run_id: str,
    state: Any,
    phase: str,
    outcome: str = "",
    checkpoint_reference: str = "",
    applied: bool = False,
) -> None:
    """Persist a best-effort V2 observation without affecting pipeline state.

    This is deliberately a write-only integration point.  Existing V2
    checkpoints, candidate selection and publication gates remain authoritative.
    """
    media = getattr(state, "media", None)
    current_metrics = {
        "required": int(getattr(media, "required", 0) or 0),
        "accepted": int(getattr(media, "accepted", 0) or 0),
        "missing": int(getattr(media, "missing", 0) or 0),
        "phase": phase,
        "queries_attempted": int(getattr(getattr(media, "search", None), "queries_attempted", 0) or 0),
    }
    with connect(root) as db:
        previous = db.execute(
            "SELECT planned_deficits FROM media_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        try:
            history = json.loads(previous[0]) if previous and previous[0] else {}
        except (TypeError, ValueError):
            history = {}
        planned = {
            "initial": history.get("initial", current_metrics),
            "final": current_metrics,
        }
        db.execute(
            """INSERT INTO media_runs
            (run_id, post_id, planned_deficits, acquisition_outcome, checkpoint_reference, completion_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(run_id) DO UPDATE SET
                planned_deficits=excluded.planned_deficits,
                acquisition_outcome=excluded.acquisition_outcome,
                checkpoint_reference=excluded.checkpoint_reference,
                completion_reason=excluded.completion_reason""",
            (run_id, int(post_id), json.dumps(planned, sort_keys=True), outcome,
             checkpoint_reference, "readback_confirmed" if applied else phase),
        )
        assets = []
        inline = getattr(media, "inline", ()) if media is not None else ()
        assets.extend(inline)
        featured = getattr(media, "featured", None)
        if featured is not None and getattr(featured, "media_id", None):
            assets.append(featured)
        for item in assets:
            url = str(getattr(item, "media_url", "") or "").strip()
            if not url:
                continue
            db.execute(
                """INSERT INTO media_assets
                (canonical_url, source_type, sha256, phash, visual_group_id,
                 wordpress_media_id, subject, status)
                VALUES (?, 'v2_observation', ?, ?, ?, ?, ?, 'known')
                ON CONFLICT(canonical_url) DO UPDATE SET
                    sha256=CASE WHEN excluded.sha256 <> '' THEN excluded.sha256 ELSE media_assets.sha256 END,
                    phash=CASE WHEN excluded.phash <> '' THEN excluded.phash ELSE media_assets.phash END,
                    visual_group_id=CASE WHEN excluded.visual_group_id <> '' THEN excluded.visual_group_id ELSE media_assets.visual_group_id END,
                    wordpress_media_id=COALESCE(excluded.wordpress_media_id, media_assets.wordpress_media_id),
                    subject=CASE WHEN excluded.subject <> '' THEN excluded.subject ELSE media_assets.subject END""",
                (url, str(getattr(item, "sha256", "") or ""), str(getattr(item, "phash", "") or ""),
                 str(getattr(item, "visual_group_id", "") or ""), getattr(item, "media_id", None),
                 str(getattr(item, "subject", "") or "")),
            )
            asset_id = db.execute(
                "SELECT asset_id FROM media_assets WHERE canonical_url = ?", (url,)
            ).fetchone()[0]
            role = "featured" if item is featured else "inline"
            db.execute(
                """INSERT INTO media_usage
                (post_id, asset_id, wordpress_media_id, role, section_slot, status, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)
                ON CONFLICT(post_id, asset_id, role, COALESCE(section_slot, -1)) DO UPDATE SET
                    status=CASE WHEN media_usage.status = 'applied' THEN 'applied' ELSE excluded.status END,
                    applied_at=COALESCE(excluded.applied_at, media_usage.applied_at)""",
                (int(post_id), asset_id, getattr(item, "media_id", None), role,
                 getattr(item, "section_slot", None), "applied" if applied else "accepted", applied),
            )
        db.execute(
            """INSERT INTO media_decisions
            (post_id, run_id, candidate_id, role, subject, reason_code, disposition, scope, evidence_reference, created_at)
            VALUES (?, ?, '', 'run', '', ?, ?, 'post', ?, CURRENT_TIMESTAMP)""",
            (int(post_id), run_id, phase, outcome or phase, checkpoint_reference),
        )
        db.commit()


def load_v2_media(root: str | Path, *, post_id: int) -> list[dict[str, Any]]:
    """Return previously accepted/applied media for resolver-side reuse."""
    with connect(root) as db:
        rows = db.execute(
            """SELECT a.wordpress_media_id, a.canonical_url, a.sha256, a.phash,
                      a.visual_group_id, a.subject, u.role, u.section_slot, u.status
               FROM media_usage AS u
               JOIN media_assets AS a ON a.asset_id = u.asset_id
              WHERE u.post_id = ? AND u.status IN ('accepted', 'applied')
              ORDER BY u.usage_id""",
            (int(post_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def record_v2_candidate_decision(
    root: str | Path,
    *,
    post_id: int,
    run_id: str,
    candidate: dict[str, Any],
    role: str,
    disposition: str,
    reason_code: str,
) -> None:
    """Store one sanitized candidate decision for future adaptive runs."""
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    if not candidate_id:
        return
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
    reference = json.dumps({
        "verdict": str(evidence.get("verdict") or ""),
        "source_domain": str(candidate.get("source_page_url") or "").split("/", 3)[2]
        if "://" in str(candidate.get("source_page_url") or "") else "",
    }, sort_keys=True)
    with connect(root) as db:
        db.execute(
            """INSERT INTO media_decisions
            (post_id, run_id, candidate_id, role, subject, reason_code, disposition, scope, evidence_reference, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', ?, CURRENT_TIMESTAMP)""",
            (int(post_id), run_id, candidate_id, role, str(candidate.get("subject") or ""),
             reason_code, disposition, reference),
        )
        db.commit()


__all__ = [
    "SCHEMA_VERSION", "connect", "load_v2_media", "migrate_legacy_index",
    "record_v2_candidate_decision", "record_v2_observation", "registry_path",
]
