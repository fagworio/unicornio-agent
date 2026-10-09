import copy
import json

import pytest

from unicornio_editor.config import Config
from unicornio_editor.manifest import build_ready_manifest, manifest_hash, manifest_matches, serialize_manifest
from unicornio_editor.pipeline_v2.model import (
    FeaturedProgress,
    FeaturedStatus,
    InlineMedia,
    LifecycleState,
    MediaProgress,
    Phase,
    WorkState,
)
from unicornio_editor.pipeline_v2.publication import (
    audit_publication_posts,
    repair_lost_ready_hash,
    reconcile_published_v2,
)
from unicornio_editor.workflow import _publish_now


def _ready_post(post_id=114840):
    content = "<p>conteudo pronto</p>"
    featured_media = 42
    seo = {
        "title": "Título pronto",
        "meta_description": "Descrição pronta",
        "focus_keyword": "palavra-chave",
    }
    manifest = build_ready_manifest(
        post_id=post_id,
        content=content,
        featured_media=featured_media,
        seo=seo,
        original_link="https://source.test/article",
        editorial={},
        policy_version=3,
    )
    state = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
        media=MediaProgress(
            required=1,
            inline=(InlineMedia(77, "https://cdn.test/inline.webp", 0),),
            featured=FeaturedProgress(FeaturedStatus.VALID, featured_media, "https://cdn.test/featured.webp"),
        ),
    )
    return {
        "id": post_id,
        "status": "publish",
        "date": "2026-10-08T18:00:00",
        "date_gmt": "2026-10-08T21:00:00",
        "featured_media": featured_media,
        "content": {"raw": content},
        "meta": {
            "_hermes_state": "published",
            "_hermes_work_state": json.dumps(state.to_dict(), separators=(",", ":")),
            "_hermes_ready_manifest": serialize_manifest(manifest),
            "_hermes_ready_hash": manifest_hash(manifest),
            "rank_math_title": seo["title"],
            "rank_math_description": seo["meta_description"],
            "rank_math_focus_keyword": seo["focus_keyword"],
            "original_link": "https://source.test/article",
        },
    }


class FakePublicationClient:
    def __init__(self, post):
        self.post = copy.deepcopy(post)
        self.updates = []
        self.publish_calls = []

    def get_post(self, post_id):
        assert int(post_id) == self.post["id"]
        return copy.deepcopy(self.post)

    def update_post(self, post_id, payload):
        assert int(post_id) == self.post["id"]
        self.updates.append(copy.deepcopy(payload))
        self.post["meta"].update(payload.get("meta", {}))
        return copy.deepcopy(self.post)

    def publish(self, post_id, **kwargs):
        self.publish_calls.append((post_id, kwargs))
        self.post["status"] = "publish"
        self.post["meta"].update(kwargs.get("meta", {}))
        if kwargs.get("date_gmt"):
            self.post["date_gmt"] = kwargs["date_gmt"]
        return copy.deepcopy(self.post)


def _config(*, dry_run=False, publish_enabled=True):
    return Config(
        content_source="https://source.test",
        wordpress_url="https://wp.test",
        wordpress_api_base="/wp-json/wp/v2",
        dry_run=dry_run,
        publish_enabled=publish_enabled,
    )


def test_publication_audit_is_read_only_and_requires_manifest_integrity():
    client = FakePublicationClient(_ready_post())

    report = audit_publication_posts(client, [114840])

    assert report["read_only"] is True
    assert report["eligible_for_reconciliation"] == 1
    assert report["posts"][0]["divergence"] == ["wordpress_publish_v2_ready"]
    assert report["posts"][0]["manifest"]["matches_post"] is True
    assert client.updates == []
    assert client.publish_calls == []


def test_manifest_from_previous_policy_cannot_use_cheap_publish_path():
    post = _ready_post()
    manifest = json.loads(post["meta"]["_hermes_ready_manifest"])
    assert manifest_matches(
        post, manifest, post["meta"]["_hermes_ready_hash"], policy_version=3
    )
    assert not manifest_matches(
        post, manifest, post["meta"]["_hermes_ready_hash"], policy_version=4
    )


def test_publication_reconciliation_only_writes_v2_and_reads_back(tmp_path):
    client = FakePublicationClient(_ready_post())

    result = reconcile_published_v2(client, _config(), tmp_path, [114840], apply=True)

    assert result["reconciled"] == [114840]
    assert result["skipped"] == []
    assert len(client.updates) == 1
    assert set(client.updates[0]) == {"meta"}
    assert set(client.updates[0]["meta"]) == {"_hermes_work_state"}
    assert client.publish_calls == []
    state = json.loads(client.post["meta"]["_hermes_work_state"])
    assert state["state"] == "published"
    assert state["phase"] == "publish"
    assert client.post["content"]["raw"] == "<p>conteudo pronto</p>"
    assert client.post["featured_media"] == 42


def test_publication_reconciliation_rejects_invalid_manifest(tmp_path):
    post = _ready_post()
    post["meta"]["_hermes_ready_hash"] = "wrong"
    client = FakePublicationClient(post)

    result = reconcile_published_v2(client, _config(), tmp_path, [114840], apply=True)

    assert result["reconciled"] == []
    assert result["skipped"] == [{"post_id": 114840, "reason": "precondition_not_eligible"}]
    assert client.updates == []
    assert client.publish_calls == []


def test_lost_ready_hash_repair_requires_committed_independent_journal_and_is_idempotent(tmp_path):
    post = _ready_post()
    original_hash = post["meta"]["_hermes_ready_hash"]
    manifest = json.loads(post["meta"]["_hermes_ready_manifest"])
    post["meta"]["_hermes_ready_hash"] = ""
    client = FakePublicationClient(post)
    journal_path = tmp_path / "work" / "v2-journal" / "114840.json"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text(json.dumps({
        "status": "committed",
        "post_id": 114840,
        "state": json.loads(post["meta"]["_hermes_work_state"]),
        "ready_hash": original_hash,
        "candidate_hash": manifest["content_hash"],
        "readback": True,
    }), encoding="utf-8")

    preview = repair_lost_ready_hash(client, _config(), tmp_path, [114840])
    assert preview["candidates"] == 1
    assert preview["posts"][0]["recovered_ready_hash"] == original_hash
    assert client.updates == []

    result = repair_lost_ready_hash(client, _config(), tmp_path, [114840], apply=True)
    assert result["repaired"] == [114840]
    assert client.publish_calls == []
    assert set(client.updates[-1]["meta"]) == {"_hermes_ready_hash", "_hermes_work_state"}
    assert client.post["meta"]["_hermes_ready_hash"] == original_hash

    second = repair_lost_ready_hash(client, _config(), tmp_path, [114840], apply=True)
    assert second["candidates"] == 0
    assert second["repaired"] == []
    assert len(client.updates) == 1


def test_lost_ready_hash_repair_rejects_incompatible_journal(tmp_path):
    post = _ready_post()
    post["meta"]["_hermes_ready_hash"] = ""
    client = FakePublicationClient(post)
    journal_path = tmp_path / "work" / "v2-journal" / "114840.json"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text(json.dumps({
        "status": "committed",
        "readback": True,
        "state": json.loads(post["meta"]["_hermes_work_state"]),
        "ready_hash": "not-the-manifest-hash",
        "candidate_hash": "not-the-content-hash",
    }), encoding="utf-8")

    result = repair_lost_ready_hash(client, _config(), tmp_path, [114840], apply=True)

    assert result["repaired"] == []
    assert client.updates == []


def test_publish_failure_after_wordpress_success_is_telemetrized(tmp_path):
    class FailingV2WriteClient(FakePublicationClient):
        def update_post(self, post_id, payload):
            raise RuntimeError("V2 write failed")

    client = FailingV2WriteClient(_ready_post())
    with pytest.raises(RuntimeError, match="V2 write failed"):
        _publish_now(client, _config(), 114840, root=tmp_path, integrity="manifest_match")

    assert client.post["status"] == "publish"
    assert client.publish_calls
    events = (tmp_path / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
    assert "publish_v2_sync_failed" in events
