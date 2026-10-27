"""Per-installation SQLite registry for V2 media evidence and decisions.

The registry is intentionally local to ``root``. It stores operational
evidence, never WordPress lifecycle state, and can be rebuilt idempotently from
the legacy media index without copying a production database into another
environment.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 5


def _utc_timestamp() -> str:
    """Return a sortable UTC instant with microsecond precision for new evidence."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
    try:
        _migrate(db)
    except Exception:
        db.close()
        raise
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
            subject TEXT NOT NULL DEFAULT '',
            identity_evidence TEXT NOT NULL DEFAULT '{}',
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
        CREATE TABLE IF NOT EXISTS media_operations (
            operation_id INTEGER PRIMARY KEY,
            post_id INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            cycle_id TEXT NOT NULL DEFAULT '',
            cycle_round INTEGER,
            authorization_id TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL DEFAULT '',
            operation_status TEXT NOT NULL DEFAULT 'started',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            write_started INTEGER NOT NULL DEFAULT 0,
            readback_confirmed INTEGER NOT NULL DEFAULT 0,
            outcome TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_media_operations_run ON media_operations(run_id);
        CREATE INDEX IF NOT EXISTS idx_media_operations_post ON media_operations(post_id);
        CREATE INDEX IF NOT EXISTS idx_media_operations_round ON media_operations(cycle_id, cycle_round, authorization_id);
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
        usage_columns_after = {
            row[1] for row in db.execute("PRAGMA table_info(media_usage)")
        }
        if "identity_evidence" not in usage_columns_after:
            db.execute(
                "ALTER TABLE media_usage ADD COLUMN identity_evidence TEXT NOT NULL DEFAULT '{}'"
            )
        usage_columns_after = {
            row[1] for row in db.execute("PRAGMA table_info(media_usage)")
        }
        if "subject" not in usage_columns_after:
            db.execute(
                "ALTER TABLE media_usage ADD COLUMN subject TEXT NOT NULL DEFAULT ''"
            )
        usage_columns_after = {
            row[1] for row in db.execute("PRAGMA table_info(media_usage)")
        }
        usage_additions = {
            "run_id": "TEXT NOT NULL DEFAULT ''",
            "cycle_id": "TEXT NOT NULL DEFAULT ''",
            "cycle_round": "INTEGER",
            "authorization_id": "TEXT NOT NULL DEFAULT ''",
            "accepted_at": "TEXT",
            "readback_confirmed_at": "TEXT",
        }
        for name, definition in usage_additions.items():
            if name not in usage_columns_after:
                db.execute(f"ALTER TABLE media_usage ADD COLUMN {name} {definition}")
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
    cycle_id: str = "",
    cycle_round: int | None = None,
    authorization_id: str = "",
    operation_status: str = "observed",
    write_started: bool = False,
    readback_confirmed: bool = False,
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
    observed_at = _utc_timestamp()
    with connect(root) as db:
        if str(run_id or "").strip():
            db.execute(
                """INSERT INTO media_operations
                (post_id, run_id, cycle_id, cycle_round, authorization_id, phase,
                 operation_status, started_at, completed_at, write_started,
                 readback_confirmed, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                        CASE WHEN ? IN ('completed', 'failed') THEN ? ELSE NULL END,
                        ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    cycle_id=CASE WHEN excluded.cycle_id <> '' THEN excluded.cycle_id ELSE media_operations.cycle_id END,
                    cycle_round=COALESCE(excluded.cycle_round, media_operations.cycle_round),
                    authorization_id=CASE WHEN excluded.authorization_id <> '' THEN excluded.authorization_id ELSE media_operations.authorization_id END,
                    phase=CASE WHEN excluded.phase <> '' THEN excluded.phase ELSE media_operations.phase END,
                    operation_status=excluded.operation_status,
                    completed_at=COALESCE(excluded.completed_at, media_operations.completed_at),
                    write_started=MAX(media_operations.write_started, excluded.write_started),
                    readback_confirmed=MAX(media_operations.readback_confirmed, excluded.readback_confirmed),
                    outcome=CASE WHEN excluded.outcome <> '' THEN excluded.outcome ELSE media_operations.outcome END""",
                (
                    int(post_id), str(run_id), str(cycle_id or ""),
                    cycle_round, str(authorization_id or ""), str(phase or ""),
                    str(operation_status or "observed"), observed_at,
                    str(operation_status or ""), observed_at,
                    int(bool(write_started)), int(bool(readback_confirmed)), str(outcome or ""),
                ),
            )
        previous = db.execute(
            "SELECT planned_deficits FROM media_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        try:
            history = json.loads(previous[0]) if previous and previous[0] else {}
        except (TypeError, ValueError):
            history = {}
        initial_metrics = history.get("initial") if isinstance(history, dict) else None
        if not isinstance(initial_metrics, dict):
            initial_metrics = current_metrics
        try:
            initial_queries = max(0, int(initial_metrics.get("queries_attempted") or 0))
            current_queries = max(0, int(current_metrics.get("queries_attempted") or 0))
        except (TypeError, ValueError):
            initial_queries = current_queries = 0
        queries_attempted = max(0, current_queries - initial_queries)
        planned = {
            "initial": history.get("initial", current_metrics),
            "final": current_metrics,
        }
        db.execute(
            """INSERT INTO media_runs
            (run_id, post_id, planned_deficits, queries_attempted, acquisition_outcome, checkpoint_reference, completion_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                planned_deficits=excluded.planned_deficits,
                queries_attempted=excluded.queries_attempted,
                acquisition_outcome=excluded.acquisition_outcome,
                checkpoint_reference=excluded.checkpoint_reference,
                completion_reason=excluded.completion_reason""",
            (run_id, int(post_id), json.dumps(planned, sort_keys=True), queries_attempted, outcome,
             checkpoint_reference, "readback_confirmed" if applied else phase, observed_at),
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
            visual_verification = getattr(item, "visual_verification", {})
            if not isinstance(visual_verification, dict):
                visual_verification = {}
            identity_evidence = json.dumps(visual_verification, sort_keys=True)
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
                (post_id, asset_id, wordpress_media_id, role, section_slot,
                 subject, identity_evidence, status, applied_at, run_id,
                 cycle_id, cycle_round, authorization_id, accepted_at,
                 readback_confirmed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? THEN ? ELSE NULL END,
                        ?, ?, ?, ?, ?,
                        CASE WHEN ? THEN ? ELSE NULL END)
                ON CONFLICT(post_id, asset_id, role, COALESCE(section_slot, -1)) DO UPDATE SET
                    status=CASE WHEN media_usage.status = 'applied' THEN 'applied' ELSE excluded.status END,
                    applied_at=COALESCE(excluded.applied_at, media_usage.applied_at),
                    wordpress_media_id=COALESCE(excluded.wordpress_media_id, media_usage.wordpress_media_id),
                    subject=CASE WHEN excluded.subject <> '' THEN excluded.subject ELSE media_usage.subject END,
                    identity_evidence=CASE WHEN excluded.identity_evidence <> '{}' THEN excluded.identity_evidence ELSE media_usage.identity_evidence END,
                    run_id=CASE WHEN media_usage.run_id <> '' THEN media_usage.run_id ELSE excluded.run_id END,
                    cycle_id=CASE WHEN media_usage.cycle_id <> '' THEN media_usage.cycle_id ELSE excluded.cycle_id END,
                    cycle_round=COALESCE(media_usage.cycle_round, excluded.cycle_round),
                    authorization_id=CASE WHEN media_usage.authorization_id <> '' THEN media_usage.authorization_id ELSE excluded.authorization_id END,
                    accepted_at=COALESCE(media_usage.accepted_at, excluded.accepted_at),
                    readback_confirmed_at=COALESCE(excluded.readback_confirmed_at, media_usage.readback_confirmed_at)""",
                (int(post_id), asset_id, getattr(item, "media_id", None), role,
                 getattr(item, "section_slot", None), str(getattr(item, "subject", "") or ""),
                 identity_evidence, "applied" if applied else "accepted", applied, observed_at,
                 str(run_id or ""), str(cycle_id or ""), cycle_round, str(authorization_id or ""),
                 observed_at, bool(readback_confirmed), observed_at),
            )
        db.execute(
            """INSERT INTO media_decisions
            (post_id, run_id, candidate_id, role, subject, reason_code, disposition, scope, evidence_reference, created_at)
            VALUES (?, ?, '', 'run', '', ?, ?, 'post', ?, ?)""",
            (int(post_id), run_id, phase, outcome or phase, checkpoint_reference, observed_at),
        )
        db.commit()


def load_v2_media(root: str | Path, *, post_id: int) -> list[dict[str, Any]]:
    """Return previously accepted/applied media for resolver-side reuse."""
    with connect(root) as db:
        rows = db.execute(
            """SELECT COALESCE(u.wordpress_media_id, a.wordpress_media_id) AS wordpress_media_id,
                      a.canonical_url, a.sha256, a.phash, a.visual_group_id,
                      COALESCE(NULLIF(u.subject, ''), a.subject) AS subject,
                      u.identity_evidence, u.role, u.section_slot, u.status
               FROM media_usage AS u
               JOIN media_assets AS a ON a.asset_id = u.asset_id
              WHERE u.post_id = ? AND u.status = 'applied'
              ORDER BY u.usage_id DESC""",
            (int(post_id),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw_identity = item.pop("identity_evidence", "{}")
        try:
            identity = json.loads(raw_identity) if raw_identity else {}
        except (TypeError, ValueError):
            identity = {}
        item["visual_verification"] = identity if isinstance(identity, dict) else {}
        result.append(item)
    return result


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
    vision = evidence.get("vision_decision") if isinstance(evidence.get("vision_decision"), dict) else {}
    rights = candidate.get("rights_decision") if isinstance(candidate.get("rights_decision"), dict) else {}
    direct_url = str(candidate.get("direct_image_url") or "").strip()
    source_page_url = str(candidate.get("source_page_url") or "").strip()
    matched_fields = {
        "filename", "og_title", "page_title", "alt_original", "figcaption",
        "heading", "page_url", "query",
    }
    allowed_reason_codes = {
        "candidate_accepted", "transient_provider_failure", "candidate_rejected",
        "source_verification_inconclusive", "visual_identity_pending",
        "unresolved_source", "reject",
        "incomplete_provenance", "ambiguous", "vision_input_unavailable",
        "source_mismatch", "source_bytes_mismatch",
        "source_page_unavailable", "source_asset_unavailable",
        "source_scan_incomplete", "source_decode_failed",
        "candidate_asset_unavailable", "insufficient_source_context",
        "subject_mismatch", "role_unsuitable", "visual_identity_unverified",
        "baseline_identity_unavailable",
        "duplicate_existing_frame", "duplicate_frame", "unrelated",
        "width_below_minimum", "resolution", "identity_mismatch",
        "vision_provider_error", "media_rights_unproven", "media_rights_incompatible",
        "media_rights_unavailable", "media_rights_unsupported_evidence",
    }
    matched = evidence.get("matched")
    penalties = evidence.get("penalties")
    decision_causes = list(dict.fromkeys(
        str(value or "").casefold().strip()
        for value in (
            rights.get("reason_code"),
            candidate.get("source_verification_reason_code"),
            candidate.get("reason_code"),
            evidence.get("reason_code"),
            evidence.get("verdict"),
        )
        if str(value or "").casefold().strip() in allowed_reason_codes
    ))
    reference = json.dumps({
        "verdict": str(evidence.get("verdict") or ""),
        "gate": str(evidence.get("gate") or "") if str(evidence.get("gate") or "") in {
            "provenance", "diversity", "relevance",
        } else "",
        "score": (
            int(evidence["score"])
            if isinstance(evidence.get("score"), int) and not isinstance(evidence.get("score"), bool)
            else None
        ),
        "local_score": (
            int(evidence["local_score"])
            if isinstance(evidence.get("local_score"), int) and not isinstance(evidence.get("local_score"), bool)
            else None
        ),
        # Keep only field labels, not the text that matched them; titles,
        # queries and alt text can contain private or user-supplied content.
        "matched": sorted({str(field) for field in matched if str(field) in matched_fields})
        if isinstance(matched, (list, tuple, set)) else [],
        "penalties": sorted({
            str(value)[:40] for value in penalties
            if isinstance(value, str) and value.replace("_", "").isalnum()
        }) if isinstance(penalties, (list, tuple, set)) else [],
        "source_comparison_proved": candidate.get("source_comparison_proved") is True
        or evidence.get("source_comparison_proved") is True,
        "recoverable": evidence.get("recoverable") is True,
        "source_domain": (urlsplit(source_page_url).hostname or "").casefold(),
        # Hash URLs rather than storing their query strings: discovery URLs can
        # contain signed parameters, but recovery needs an exact provenance
        # binding for the candidate without retaining those secrets.
        "direct_image_url_sha256": hashlib.sha256(direct_url.encode()).hexdigest() if direct_url else "",
        "source_page_url_sha256": hashlib.sha256(source_page_url.encode()).hexdigest() if source_page_url else "",
        # Persist a compact, non-sensitive model judgment for later audit.
        # Never store image payloads, prompt/context text, signed URLs, or raw
        # provider errors in the registry evidence reference.
        "vision_decision": {
            key: vision[key]
            for key in (
                "status", "confidence", "visual_type", "relation", "editorial_fit",
                "role_fit", "dimensions_source", "verdict", "detail_used",
                "low_status", "low_confidence", "low_visual_type", "image_sha256",
                "image_role", "policy_version", "decision_source", "model_identity", "context_key", "subject",
            )
            if key in vision
        },
        "vision_policy_version": str(evidence.get("vision_policy_version") or ""),
        "rights_decision": {
            key: rights[key]
            for key in (
                "status", "reason_code", "basis", "retryable", "policy_version", "stage",
                "evidence_reference",
            )
            if key in rights
        },
        "decision_causes": decision_causes,
    }, sort_keys=True)
    safe_reason_code = str(reason_code or "").casefold().strip()
    if safe_reason_code not in allowed_reason_codes:
        safe_reason_code = "candidate_rejected"
    created_at = _utc_timestamp()
    with connect(root) as db:
        db.execute(
            """INSERT INTO media_decisions
            (post_id, run_id, candidate_id, role, subject, reason_code, disposition, scope, evidence_reference, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)""",
            (int(post_id), run_id, candidate_id, role, str(candidate.get("subject") or ""),
             safe_reason_code, disposition, reference, created_at),
        )
        db.commit()


def invalidate_v2_media(
    root: str | Path,
    *,
    post_id: int,
    media_ids: list[int],
    role: str | None = None,
) -> None:
    """Retain history while removing explicitly invalidated uses from reuse."""
    ids = sorted({int(value) for value in media_ids if int(value) > 0})
    if not ids:
        return
    with connect(root) as db:
        placeholders = ",".join("?" for _ in ids)
        role_clause = " AND role=?" if role else ""
        params: tuple[Any, ...] = (int(post_id), *ids, *((str(role),) if role else ()))
        db.execute(
            f"UPDATE media_usage SET status='invalidated' WHERE post_id=? AND wordpress_media_id IN ({placeholders}){role_clause}",
            params,
        )
        db.commit()


__all__ = [
    "SCHEMA_VERSION", "connect", "load_v2_media", "migrate_legacy_index",
    "record_v2_candidate_decision", "record_v2_observation", "registry_path",
    "invalidate_v2_media",
]
