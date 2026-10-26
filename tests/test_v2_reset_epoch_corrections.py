import json
import sqlite3
from pathlib import Path

from unicornio_editor.pipeline_v2.model import LifecycleState, Phase, WorkState
from unicornio_editor.pipeline_v2.reset_epoch import _active_artifact_roots, _classify, _human_required_audit, _initial_state, _registry_projection


def test_active_artifact_roots_include_real_root_backups(tmp_path):
    (tmp_path / "backups" / "12").mkdir(parents=True)
    (tmp_path / "backups" / "12" / "editorial.draft.json").write_text("{}")
    roots = _active_artifact_roots(tmp_path)
    assert tmp_path / "backups" in roots


def test_registry_projection_excludes_post_scoped_decisions(tmp_path):
    source = tmp_path / "source.sqlite"
    target = tmp_path / "target.sqlite"
    con = sqlite3.connect(source)
    con.executescript("""
      create table media_assets(asset_id integer primary key, canonical_url text not null, source_page_url text not null, source_type text not null, sha256 text not null, phash text not null, visual_group_id text not null, width integer, height integer, mime_type text not null, wordpress_media_id integer, subject text not null, article_id integer, legacy_uses integer not null default 0, provenance_evidence text not null, identity_evidence text not null, status text not null default 'known', policy_version text not null);
      create table media_decisions(decision_id integer primary key, post_id integer not null, run_id text not null, candidate_id text not null, asset_id integer, role text not null, subject text not null, reason_code text not null, disposition text not null, scope text not null default 'post', retry_after text, evidence_reference text not null, created_at text not null);
      create table schema_meta(key text primary key, value text not null);
      insert into media_assets values(1,'u','s','x','h','p','g',1,1,'image/jpeg',9,'subject',null,0,'{}','{}','known','v1');
      insert into media_decisions values(1,12,'run','c',1,'inline','subject','match','accepted','post',null,'e','2026-01-01');
      insert into media_decisions values(2,0,'','c',1,'asset','subject','match','accepted','asset',null,'e','2026-01-01');
    """)
    con.commit(); con.execute("pragma journal_mode=wal"); con.commit(); con.close()
    copied = _registry_projection(source, target, "epoch")
    out = sqlite3.connect(target)
    assert out.execute("select count(*) from media_assets").fetchone()[0] == 1
    assert out.execute("select count(*) from media_decisions").fetchone()[0] == 1
    assert copied["media_decisions"] == 1
    assert out.execute("pragma integrity_check").fetchone()[0] == "ok"
    out.close()


def test_initial_state_is_clean():
    state = _initial_state()
    assert state.state is LifecycleState.PENDING
    assert state.phase is Phase.RELEVANCE
    assert state.retry.attempts == 0
