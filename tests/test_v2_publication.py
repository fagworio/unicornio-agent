import copy
import hashlib
import json
from unittest import mock

import pytest

from unicornio_editor.config import Config
from unicornio_editor.manifest import build_ready_manifest, manifest_hash, manifest_matches, serialize_manifest
from unicornio_editor.locking import LockError, LockManager
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
from unicornio_editor.workflow import (
    META_PUBLISH_OPERATION_ID,
    _publish_now,
    _publish_post_unlocked,
    _guard_post_client,
    _reconcile_uncertain_publication,
    _reconcile_persisted_uncertain_publications,
    publish_ready_posts,
)
from unicornio_editor.wordpress import WordPressClient


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

    def list_pending(self, page=1, per_page=100):
        return []


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


def test_publish_preflight_refuses_concurrent_wordpress_edit_without_write(tmp_path):
    expected = _pending_ready_post()

    class ConcurrentEditClient(FakePublicationClient):
        def __init__(self, post):
            super().__init__(post)
            self.post["content"]["raw"] = "human edit before guard"

    client = ConcurrentEditClient(expected)
    result = _publish_now(
        client,
        _config(),
        expected["id"],
        root=tmp_path,
        integrity="manifest_match",
        expected_post=expected,
    )
    assert result["status"] == "retry"
    assert "changed during publish preflight" in result["reason"]
    assert client.publish_calls == []


def test_lost_post_lock_blocks_guarded_wordpress_mutation(tmp_path):
    client = FakePublicationClient(_pending_ready_post())
    lock_root = tmp_path / "locks"
    handle = LockManager(lock_root, ttl=60).acquire(client.post["id"])
    guarded = _guard_post_client(client, handle)
    with handle:
        replacement = lock_root / "replacement.lock"
        replacement.write_text('{"token":"new-owner","released":true}', encoding="utf-8")
        replacement.replace(lock_root / f"{client.post['id']}.lock")
        with pytest.raises(LockError, match="replaced"):
            guarded.update_post(client.post["id"], {"meta": {"_hermes_state": "blocked"}})
    assert client.updates == []


def test_wordpress_transport_rechecks_post_lock_immediately_before_request(tmp_path):
    client = WordPressClient(_config())
    handle = LockManager(tmp_path / "locks", ttl=60).acquire(42)
    guarded = _guard_post_client(client, handle)

    with handle:
        with mock.patch.object(
            handle,
            "assert_owned",
            side_effect=[None, None, LockError("ownership lost at transport boundary")],
        ) as assert_owned, mock.patch("unicornio_editor.wordpress.urlopen") as open_url:
            with pytest.raises(LockError, match="transport boundary"):
                guarded.update_post(42, {"title": "test"})

    assert assert_owned.call_count == 3
    open_url.assert_not_called()


def test_publish_queue_page_cap_is_explicit_and_precedes_post_writes(tmp_path):
    class QueueClient:
        def __init__(self):
            self.published = []

        def list_pending(self, *, page, per_page):
            return [{"id": page * 100 + index} for index in range(100)] if page <= 100 else [{"id": 99999}]

    from unicornio_editor.workflow import WorkflowError

    client = QueueClient()
    with pytest.raises(WorkflowError, match="resume scan at page 101"):
        publish_ready_posts(client, _config(), tmp_path, limit=1)
    assert client.published == []


def _pending_ready_post():
    post = _ready_post()
    post["status"] = "pending"
    state = json.loads(post["meta"]["_hermes_work_state"])
    state["state"] = "ready"
    state["phase"] = "validate"
    post["meta"]["_hermes_state"] = "ready"
    post["meta"]["_hermes_work_state"] = json.dumps(state, separators=(",", ":"))
    return post


def test_publish_timeout_after_commit_reconciles_without_second_publish(tmp_path):
    class TimeoutAfterCommitClient(FakePublicationClient):
        def publish(self, post_id, **kwargs):
            result = super().publish(post_id, **kwargs)
            raise TimeoutError("read operation timed out")

    client = TimeoutAfterCommitClient(_pending_ready_post())
    post = client.post
    result = _publish_now(
        client,
        _config(),
        post["id"],
        root=tmp_path,
        integrity="manifest_match",
        ready_hash=post["meta"]["_hermes_ready_hash"],
        ready_manifest=post["meta"]["_hermes_ready_manifest"],
    )

    assert result["status"] == "published"
    assert result["reconciled"] is True
    assert len(client.publish_calls) == 1
    assert json.loads(client.post["meta"]["_hermes_work_state"])["state"] == "published"
    assert not (tmp_path / "work" / "v2-publication-uncertain" / f"{post['id']}.json").exists()
    events = (tmp_path / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
    assert "post_publication_reconciled" in events
    assert "post_published_reconciled" in events
    assert '"event": "post_published"' not in events


def test_publish_intent_is_durable_before_remote_write_and_recovers_process_crash(tmp_path):
    class CrashAfterCommitClient(FakePublicationClient):
        def publish(self, post_id, **kwargs):
            marker = tmp_path / "work" / "v2-publication-uncertain" / f"{post_id}.json"
            intent = json.loads(marker.read_text(encoding="utf-8"))
            operation_id = kwargs["meta"][META_PUBLISH_OPERATION_ID]
            assert intent["status"] == "intent"
            assert intent["intent_persisted"] is True
            assert intent["publication_operation_id"] == operation_id
            super().publish(post_id, **kwargs)
            # Simulate process death after WordPress commits but before the
            # caller can receive a response or replace the local intent.
            raise SystemExit("simulated abrupt process termination")

    client = CrashAfterCommitClient(_pending_ready_post())
    post = client.post
    with pytest.raises(SystemExit, match="simulated abrupt"):
        _publish_now(
            client,
            _config(),
            post["id"],
            root=tmp_path,
            integrity="manifest_match",
            ready_hash=post["meta"]["_hermes_ready_hash"],
            ready_manifest=post["meta"]["_hermes_ready_manifest"],
        )

    marker = tmp_path / "work" / "v2-publication-uncertain" / f"{post['id']}.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "intent"
    outcomes = _reconcile_persisted_uncertain_publications(
        client, _config(), tmp_path
    )

    assert [item["status"] for item in outcomes] == ["published"]
    assert len(client.publish_calls) == 1
    assert not marker.exists()


def test_publish_is_not_sent_when_intent_cannot_be_persisted(tmp_path):
    client = FakePublicationClient(_pending_ready_post())
    post = client.post
    with mock.patch(
        "unicornio_editor.workflow.atomic_write_text",
        side_effect=OSError("disk full"),
    ):
        result = _publish_now(
            client,
            _config(),
            post["id"],
            root=tmp_path,
            integrity="manifest_match",
            ready_hash=post["meta"]["_hermes_ready_hash"],
            ready_manifest=post["meta"]["_hermes_ready_manifest"],
        )

    assert result["status"] == "retry"
    assert "write não enviado" in result["reason"]
    assert client.publish_calls == []
    assert client.post["status"] == "pending"


def test_publish_reconciles_missing_legacy_state_without_second_publish(tmp_path):
    class LegacyMetaDroppingClient(FakePublicationClient):
        def publish(self, post_id, **kwargs):
            result = super().publish(post_id, **kwargs)
            self.post["meta"].pop("_hermes_state", None)
            return result

    client = LegacyMetaDroppingClient(_pending_ready_post())
    post = client.post
    result = _publish_now(
        client,
        _config(),
        post["id"],
        root=tmp_path,
        integrity="manifest_match",
        ready_hash=post["meta"]["_hermes_ready_hash"],
        ready_manifest=post["meta"]["_hermes_ready_manifest"],
    )

    assert result["status"] == "published"
    assert len(client.publish_calls) == 1
    assert client.post["meta"]["_hermes_state"] == "published"
    assert any(
        payload.get("meta", {}).get("_hermes_state") == "published"
        for payload in client.updates
    )


def test_publish_timeout_before_commit_is_retryable_and_not_marked_published(tmp_path):
    class TimeoutBeforeCommitClient(FakePublicationClient):
        def publish(self, post_id, **kwargs):
            self.publish_calls.append((post_id, kwargs))
            raise TimeoutError("connect timed out")

    client = TimeoutBeforeCommitClient(_pending_ready_post())
    result = _publish_now(
        client,
        _config(),
        client.post["id"],
        root=tmp_path,
        integrity="manifest_match",
        ready_hash=client.post["meta"]["_hermes_ready_hash"],
        ready_manifest=client.post["meta"]["_hermes_ready_manifest"],
    )

    assert result["status"] == "retry"
    assert not result.get("uncertain", False)
    assert client.post["status"] == "pending"
    assert client.post["meta"]["_hermes_state"] == "ready"
    assert not (tmp_path / "work" / "v2-publication-uncertain" / f"{client.post['id']}.json").exists()


def test_telemetry_failure_does_not_abort_publish_transaction(tmp_path):
    client = FakePublicationClient(_pending_ready_post())
    post = client.post

    with mock.patch(
        "unicornio_editor.workflow.append_telemetry", side_effect=OSError("disk full")
    ):
        result = _publish_now(
            client,
            _config(),
            post["id"],
            root=tmp_path,
            integrity="manifest_match",
            ready_hash=post["meta"]["_hermes_ready_hash"],
            ready_manifest=post["meta"]["_hermes_ready_manifest"],
        )

    assert result["status"] == "published"
    assert client.publish_calls
    assert client.post["status"] == "publish"


def test_publisher_reconciles_uncertain_marker_even_when_post_left_pending_queue(tmp_path):
    client = FakePublicationClient(_ready_post())
    operation_id = "known-publish-operation"
    client.post["meta"][META_PUBLISH_OPERATION_ID] = operation_id
    marker = tmp_path / "work" / "v2-publication-uncertain" / f"{client.post['id']}.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "post_id": client.post["id"],
        "status": "uncertain",
        "publication_operation_id": operation_id,
    }), encoding="utf-8")

    outcomes = publish_ready_posts(client, _config(), tmp_path, limit=1)

    assert [outcome["post_id"] for outcome in outcomes] == [client.post["id"]]
    assert outcomes[0]["status"] == "published"
    assert outcomes[0]["reconciled"] is True
    assert client.publish_calls == []
    assert not marker.exists()


def test_uncertain_legacy_marker_cannot_attribute_published_status_without_operation_id(tmp_path):
    post = _ready_post()
    client = FakePublicationClient(post)
    marker = tmp_path / "work" / "v2-publication-uncertain" / f"{post['id']}.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"post_id": post["id"], "status": "uncertain"}), encoding="utf-8")

    result = _reconcile_uncertain_publication(
        client,
        _config(),
        tmp_path,
        client.post,
        post["id"],
        ready_hash=post["meta"]["_hermes_ready_hash"],
        ready_manifest=post["meta"]["_hermes_ready_manifest"],
    )

    assert result["status"] == "retry"
    assert result["uncertain"] is True
    assert "autoria" in result["reason"]
    assert client.updates == []
    assert client.publish_calls == []
    assert marker.exists()


def test_persisted_uncertain_publish_reconciles_ready_projection_with_matching_identity(tmp_path):
    post = _pending_ready_post()
    post["status"] = "publish"
    post["meta"]["_hermes_state"] = "ready"
    operation_id = "publish-op-114840-1"
    post["meta"][META_PUBLISH_OPERATION_ID] = operation_id
    client = FakePublicationClient(post)
    marker = tmp_path / "work" / "v2-publication-uncertain" / f"{post['id']}.json"
    marker.parent.mkdir(parents=True)
    manifest = post["meta"]["_hermes_ready_manifest"]
    marker.write_text(json.dumps({
        "post_id": post["id"],
        "status": "uncertain",
        "publication_operation_id": operation_id,
        "ready_hash": post["meta"]["_hermes_ready_hash"],
        "ready_manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
    }), encoding="utf-8")

    result = _reconcile_uncertain_publication(
        client,
        _config(),
        tmp_path,
        client.post,
        post["id"],
        ready_hash=post["meta"]["_hermes_ready_hash"],
        ready_manifest=manifest,
    )

    assert result["status"] == "published"
    assert result["reconciled"] is True
    assert client.post["meta"]["_hermes_state"] == "published"
    assert json.loads(client.post["meta"]["_hermes_work_state"])["state"] == "published"
    assert not marker.exists()


def test_uncertain_publication_does_not_claim_manual_publish_without_operation_id(tmp_path):
    post = _pending_ready_post()
    post["status"] = "publish"
    post["meta"]["_hermes_state"] = "ready"
    client = FakePublicationClient(post)
    operation_id = "agent-publish-attempt"
    marker = tmp_path / "work" / "v2-publication-uncertain" / f"{post['id']}.json"
    marker.parent.mkdir(parents=True)
    manifest = post["meta"]["_hermes_ready_manifest"]
    marker.write_text(json.dumps({
        "post_id": post["id"],
        "status": "uncertain",
        "publication_operation_id": operation_id,
        "ready_hash": post["meta"]["_hermes_ready_hash"],
        "ready_manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
    }), encoding="utf-8")

    result = _reconcile_uncertain_publication(
        client,
        _config(),
        tmp_path,
        client.post,
        post["id"],
        ready_hash=post["meta"]["_hermes_ready_hash"],
        ready_manifest=manifest,
    )

    assert result["status"] == "retry"
    assert result["uncertain"] is True
    assert "não corresponde" in result["reason"]
    assert client.updates == []
    assert client.post["meta"]["_hermes_state"] == "ready"
    assert marker.exists()


def test_persisted_uncertain_publish_identity_mismatch_stays_fenced(tmp_path):
    post = _pending_ready_post()
    post["status"] = "publish"
    post["meta"]["_hermes_state"] = "ready"
    client = FakePublicationClient(post)
    marker = tmp_path / "work" / "v2-publication-uncertain" / f"{post['id']}.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "post_id": post["id"],
        "status": "uncertain",
        "ready_hash": "stale-hash",
        "ready_manifest_sha256": hashlib.sha256(b"stale-manifest").hexdigest(),
    }), encoding="utf-8")

    result = _reconcile_uncertain_publication(
        client,
        _config(),
        tmp_path,
        client.post,
        post["id"],
        ready_hash=post["meta"]["_hermes_ready_hash"],
        ready_manifest=post["meta"]["_hermes_ready_manifest"],
    )

    assert result["status"] == "retry"
    assert result["uncertain"] is True
    assert client.post["meta"]["_hermes_state"] == "ready"
    assert json.loads(client.post["meta"]["_hermes_work_state"])["state"] == "ready"
    assert marker.exists()


@pytest.mark.parametrize(
    ("gate", "title", "content"),
    [
        (
            "envelope",
            "Título válido",
            '{"post_id": 114840, "status": "publish", "cleaned_html": "<p>raw</p>"}',
        ),
        (
            "language",
            "A Long English Headline About a Major Video Game Release",
            "<p>This is a detailed English article about a major video game release. "
            "The story explains the new platform, launch date, characters, gameplay, "
            "development team, and the publisher's announcement for international readers.</p>",
        ),
    ],
)
def test_early_publish_gate_reopens_v2_ready_state(tmp_path, gate, title, content):
    post = _pending_ready_post()
    post["title"] = {"raw": title}
    post["content"] = {"raw": content}
    client = FakePublicationClient(post)

    result = _publish_post_unlocked(client, _config(), tmp_path, post["id"])

    state = json.loads(client.post["meta"]["_hermes_work_state"])
    assert result["status"] == "blocked"
    assert client.publish_calls == []
    assert state["state"] == "pending"
    assert state["phase"] == "editorial"
    assert state["blocker"] == "text_quality"
    assert client.post["meta"]["_hermes_state"] == "blocked"


def test_reconciled_publication_does_not_consume_new_publish_quota(tmp_path):
    class QueueClient(FakePublicationClient):
        def list_pending(self, page=1, per_page=100):
            return [
                {
                    "id": 1,
                    "status": "pending",
                    "meta": {"_hermes_state": "ready"},
                },
                {
                    "id": 2,
                    "status": "pending",
                    "meta": {"_hermes_state": "ready"},
                },
            ] if page == 1 else []

    client = QueueClient(_ready_post())
    reconciled = {
        "post_id": 999,
        "status": "published",
        "wordpress_changed": True,
        "reconciled": True,
        "publication_kind": "reconciled",
    }

    with mock.patch(
        "unicornio_editor.workflow._reconcile_persisted_uncertain_publications",
        return_value=[reconciled],
    ), mock.patch(
        "unicornio_editor.workflow.publish_post",
        side_effect=lambda _client, _config, _root, post_id: {
            "post_id": post_id,
            "status": "published",
            "wordpress_changed": True,
            "publication_kind": "new",
        },
    ) as publish:
        outcomes = publish_ready_posts(client, _config(), tmp_path, limit=1)

    assert [call.args[-1] for call in publish.call_args_list] == [1]
    assert [outcome["publication_kind"] for outcome in outcomes] == ["reconciled", "new"]


def test_persisted_uncertain_reconciliation_respects_dry_run(tmp_path):
    client = FakePublicationClient(_ready_post())
    marker = tmp_path / "work" / "v2-publication-uncertain" / f"{client.post['id']}.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"post_id": client.post["id"], "status": "uncertain"}), encoding="utf-8")

    outcomes = publish_ready_posts(client, _config(dry_run=True), tmp_path, limit=1)

    assert outcomes[0]["status"] == "skipped"
    assert outcomes[0]["dry_run"] is True
    assert marker.exists()
    assert client.updates == []
    assert client.publish_calls == []


def test_persisted_reconciliation_does_not_emit_new_publication_event(tmp_path):
    client = FakePublicationClient(_ready_post())
    operation_id = "known-publish-operation"
    client.post["meta"][META_PUBLISH_OPERATION_ID] = operation_id
    marker = tmp_path / "work" / "v2-publication-uncertain" / f"{client.post['id']}.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "post_id": client.post["id"],
        "status": "uncertain",
        "publication_operation_id": operation_id,
    }), encoding="utf-8")

    outcomes = publish_ready_posts(client, _config(), tmp_path, limit=1)

    assert outcomes[0]["publication_kind"] == "reconciled"
    events = (tmp_path / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
    assert '"event": "post_published_reconciled"' in events
    assert '"event": "post_published"' not in events
