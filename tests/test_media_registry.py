import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from unicornio_editor.media.registry import (
    RegistryError, connect, invalidate_v2_media, load_v2_media, migrate_legacy_index,
    record_v2_candidate_decision, record_v2_observation, registry_path,
)
from unicornio_editor.pipeline_v2.model import InlineMedia, MediaProgress, FeaturedProgress, FeaturedStatus, MediaSearchProgress
from unicornio_editor.pipeline_v2.runtime import _attach_candidate_vision_evidence


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


def test_candidate_decision_persists_hashed_provenance_without_signed_url(tmp_path):
    direct_url = "https://cdn.test/image.webp?token=private-signature"
    source_url = "https://private-user:private-pass@source.test/article?session=private"
    record_v2_candidate_decision(
        tmp_path, post_id=9, run_id="run-9",
        candidate={
            "candidate_id": "candidate-9", "subject": "Jogo",
            "source_page_url": source_url, "direct_image_url": direct_url,
            "evidence": {
                "verdict": "deterministic_match",
                "score": 9,
                "local_score": 5,
                "gate": "relevance",
                "matched": ["page_title", "query", "private matching text"],
                "penalties": ["not_in_source", "private detail"],
                "reason": "private source text must not be persisted",
                "source_comparison_proved": True,
                "evidence": {"page_title": "private title"},
                "vision_policy_version": "featured-editorial-v3-multidimensional",
                "vision_decision": {
                    "status": "MATCH", "confidence": 0.91, "visual_type": "key_art",
                    "relation": "direct", "editorial_fit": "suitable", "role_fit": "suitable",
                    "verdict": "accept", "detail_used": "high",
                    "image_sha256": "a" * 64, "image_role": "featured",
                    "policy_version": "featured-editorial-v3-multidimensional",
                    "model_identity": "https://vision.example/model-a",
                    "context_key": "game",
                    "prompt": "must not persist", "raw_payload": "must not persist",
                },
            },
        }, role="inline", disposition="accepted", reason_code="candidate_accepted",
    )

    with connect(tmp_path) as db:
        row = db.execute("SELECT evidence_reference FROM media_decisions").fetchone()
    reference = json.loads(row[0])
    assert reference["direct_image_url_sha256"] == hashlib.sha256(direct_url.encode()).hexdigest()
    assert reference["source_page_url_sha256"] == hashlib.sha256(source_url.encode()).hexdigest()
    assert "private" not in row[0]
    assert reference["source_domain"] == "source.test"
    assert reference["score"] == 9
    assert reference["local_score"] == 5
    assert reference["gate"] == "relevance"
    assert reference["matched"] == ["page_title", "query"]
    assert reference["penalties"] == ["not_in_source"]
    assert reference["source_comparison_proved"] is True
    assert "must not persist" not in row[0]
    assert "private title" not in row[0]
    assert "private matching text" not in row[0]
    assert reference["vision_decision"]["confidence"] == 0.91
    assert reference["vision_decision"]["image_sha256"] == "a" * 64
    assert reference["vision_decision"]["context_key"] == "game"
    assert reference["vision_decision"]["relation"] == "direct"
    assert reference["vision_decision"]["editorial_fit"] == "suitable"
    assert reference["vision_decision"]["role_fit"] == "suitable"


def test_preflight_vision_evidence_stays_correlated_to_candidate_run(tmp_path):
    candidate = {
        "candidate_id": "featured-candidate-b", "subject": "Xbox Live",
        "direct_image_url": "https://cdn.test/b.webp",
        "source_page_url": "https://source.test/article",
    }
    vision = {
        "status": "UNRELATED", "confidence": 0.80, "visual_type": "photograph",
        "verdict": "reject", "image_sha256": "b" * 64, "image_role": "featured",
        "policy_version": "featured-editorial-v3-multidimensional", "decision_source": "vision_provider",
        "model_identity": "https://vision.test/model", "context_key": "game",
        "subject": "Xbox Live",
    }
    _attach_candidate_vision_evidence(candidate, vision)
    record_v2_candidate_decision(
        tmp_path, post_id=115286, run_id="run-115286-a",
        candidate=candidate, role="featured", disposition="reject_final",
        reason_code="UNRELATED 0.80 https://cdn.test/b.webp?token=private",
    )
    with connect(tmp_path) as db:
        row = db.execute(
            "SELECT post_id, run_id, candidate_id, reason_code, evidence_reference "
            "FROM media_decisions"
        ).fetchone()
    reference = json.loads(row["evidence_reference"])
    assert (row["post_id"], row["run_id"], row["candidate_id"]) == (
        115286, "run-115286-a", "featured-candidate-b",
    )
    assert row["reason_code"] == "candidate_rejected"
    assert reference["vision_decision"]["image_sha256"] == "b" * 64
    assert reference["vision_decision"]["confidence"] == 0.80
    assert "private" not in row["evidence_reference"]


def test_candidate_diagnostics_preserve_safe_source_decision_reason(tmp_path):
    record_v2_candidate_decision(
        tmp_path,
        post_id=12,
        run_id="run-source-reason",
        candidate={
            "candidate_id": "candidate-source-reason",
            "subject": "Game",
            "source_page_url": "https://source.example/article?token=private",
            "direct_image_url": "https://cdn.example/image.webp?sig=private",
            "evidence": {
                "verdict": "unresolved_source",
                "score": 0,
                "gate": "provenance",
                "matched": [],
            },
        },
        role="inline",
        disposition="defer_retry",
        reason_code="unresolved_source",
    )
    with connect(tmp_path) as db:
        row = db.execute(
            "SELECT reason_code, evidence_reference FROM media_decisions WHERE run_id=?",
            ("run-source-reason",),
        ).fetchone()
    assert row[0] == "unresolved_source"
    assert json.loads(row[1])["verdict"] == "unresolved_source"
    assert "private" not in row[1]


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
        assert migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "4"
        migrated.execute("INSERT INTO media_assets(asset_id, canonical_url) VALUES (3, 'https://cdn.test/b')")
        migrated.execute("INSERT INTO media_usage(post_id, asset_id, role) VALUES (1, 3, 'inline')")
        with pytest.raises(Exception):
            migrated.execute("INSERT INTO media_usage(post_id, asset_id, role) VALUES (1, 3, 'inline')")


def test_v2_observation_separates_state_readback_from_media_application(tmp_path):
    media = MediaProgress(
        required=2,
        inline=(InlineMedia(
            7, "https://cdn.test/inline", 0, "alt", "credit", "subject",
            visual_verification={"decision": "DIFFERENT", "method": "test"},
        ),),
        featured=FeaturedProgress(
            FeaturedStatus.VALID, 8, "https://cdn.test/featured",
            visual_verification={"decision": "INITIAL", "method": "test"},
        ),
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
    by_role = {row["role"]: row for row in reused}
    assert by_role["inline"]["visual_verification"] == {"decision": "DIFFERENT", "method": "test"}
    assert by_role["featured"]["visual_verification"] == {"decision": "INITIAL", "method": "test"}


def test_media_run_persists_query_attempt_delta_not_cumulative_count(tmp_path):
    initial_media = MediaProgress(search=MediaSearchProgress(queries_attempted=4))
    initial_state = SimpleNamespace(media=initial_media)
    record_v2_observation(
        tmp_path, post_id=11, run_id="run-query-delta", state=initial_state,
        phase="media", outcome="started",
    )

    final_media = replace(
        initial_media,
        search=MediaSearchProgress(queries_attempted=6),
    )
    record_v2_observation(
        tmp_path, post_id=11, run_id="run-query-delta",
        state=SimpleNamespace(media=final_media), phase="media", outcome="retry",
    )

    with connect(tmp_path) as db:
        row = db.execute(
            "SELECT queries_attempted, planned_deficits FROM media_runs WHERE run_id=?",
            ("run-query-delta",),
        ).fetchone()
    assert row[0] == 2
    history = json.loads(row[1])
    assert history["initial"]["queries_attempted"] == 4
    assert history["final"]["queries_attempted"] == 6


def test_visual_identity_evidence_is_scoped_to_post_usage(tmp_path):
    shared_url = "https://cdn.test/shared.webp"
    state_a = SimpleNamespace(media=MediaProgress(
        required=1,
        inline=(InlineMedia(
            101, shared_url, 0, "alt", "credit", "Subject A",
            visual_verification={"decision": "DIFFERENT", "duplicate_of": "101"},
        ),),
    ))
    state_b = SimpleNamespace(media=MediaProgress(
        required=1,
        inline=(InlineMedia(
            202, shared_url, 0, "alt", "credit", "Subject B",
            visual_verification={"decision": "SAME_ART_CROP", "duplicate_of": "202"},
        ),),
    ))

    record_v2_observation(tmp_path, post_id=101, run_id="run-a", state=state_a, phase="validate", applied=True)
    record_v2_observation(tmp_path, post_id=202, run_id="run-b", state=state_b, phase="validate", applied=True)

    media_a = load_v2_media(tmp_path, post_id=101)[0]
    media_b = load_v2_media(tmp_path, post_id=202)[0]
    assert media_a["wordpress_media_id"] == 101
    assert media_a["subject"] == "Subject A"
    assert media_a["visual_verification"] == {"decision": "DIFFERENT", "duplicate_of": "101"}
    assert media_b["wordpress_media_id"] == 202
    assert media_b["subject"] == "Subject B"
    assert media_b["visual_verification"] == {"decision": "SAME_ART_CROP", "duplicate_of": "202"}


def test_invalidation_can_be_scoped_to_inline_role(tmp_path):
    media = MediaProgress(
        required=1,
        inline=(InlineMedia(55, "https://cdn.test/inline.webp", 0),),
        featured=FeaturedProgress(FeaturedStatus.VALID, 55, "https://cdn.test/featured.webp"),
    )
    record_v2_observation(
        tmp_path,
        post_id=55,
        run_id="run-role-scope",
        state=SimpleNamespace(media=media),
        phase="validate",
        applied=True,
    )

    invalidate_v2_media(tmp_path, post_id=55, media_ids=[55], role="inline")

    reused = load_v2_media(tmp_path, post_id=55)
    assert [(row["role"], row["canonical_url"]) for row in reused] == [
        ("featured", "https://cdn.test/featured.webp"),
    ]


def test_registry_migrates_v2_applied_usage_without_losing_readback(tmp_path):
    import sqlite3

    path = registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES ('schema_version', '2');
        CREATE TABLE media_assets (
            asset_id INTEGER PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE,
            source_page_url TEXT NOT NULL DEFAULT '', source_type TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL DEFAULT '', phash TEXT NOT NULL DEFAULT '',
            visual_group_id TEXT NOT NULL DEFAULT '', width INTEGER, height INTEGER,
            mime_type TEXT NOT NULL DEFAULT '', wordpress_media_id INTEGER,
            subject TEXT NOT NULL DEFAULT '', article_id INTEGER,
            legacy_uses INTEGER NOT NULL DEFAULT 0, provenance_evidence TEXT NOT NULL DEFAULT '{}',
            identity_evidence TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'known',
            policy_version TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO media_assets(asset_id, canonical_url, wordpress_media_id, subject)
        VALUES (7, 'https://cdn.test/legacy.webp', 700, 'Legacy subject');
        CREATE TABLE media_usage (
            post_id INTEGER NOT NULL, asset_id INTEGER NOT NULL,
            wordpress_media_id INTEGER, role TEXT NOT NULL,
            list_item TEXT NOT NULL DEFAULT '', section_slot INTEGER,
            status TEXT NOT NULL DEFAULT 'accepted', applied_at TEXT,
            usage_id INTEGER PRIMARY KEY
        );
        INSERT INTO media_usage(post_id, asset_id, wordpress_media_id, role, status, applied_at)
        VALUES (115234, 7, NULL, 'inline', 'applied', '2026-10-13 00:00:00');
    """)
    db.commit()
    db.close()

    with connect(tmp_path) as migrated:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(media_usage)")}
        assert {"identity_evidence", "subject"} <= columns
        assert migrated.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "4"
        row = migrated.execute(
            "SELECT status, applied_at FROM media_usage WHERE post_id=115234"
        ).fetchone()
        assert tuple(row) == ("applied", "2026-10-13 00:00:00")

    reused = load_v2_media(tmp_path, post_id=115234)
    assert reused[0]["wordpress_media_id"] == 700
    assert reused[0]["subject"] == "Legacy subject"
