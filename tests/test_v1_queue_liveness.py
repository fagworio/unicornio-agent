import pytest

import test_batch as fixtures
from unicornio_editor.batch import prepare_batch
from unicornio_editor.cli import _apply_editorial_batch
from unicornio_editor.config import Config
from unicornio_editor.locking import LockManager
from unicornio_editor.workflow import build_cards


class Client(fixtures.BatchClient):
    def list_pending(self, *, page=1, per_page=20, include=None, status="pending"):
        posts = [p for p in self.posts.values() if p["status"] == status and (include is None or p["id"] in include)]
        start = (page - 1) * per_page
        return posts[start:start + per_page]


@pytest.fixture
def configure():
    return Config("wordpress", "https://wp.test", "/wp-json/wp/v2", dry_run=False)


def make(post_id, state="new"):
    p = fixtures._post(post_id, "Notícia sobre videogame")
    p["meta"]["_hermes_state"] = state
    p["modified_gmt"] = "2026-11-24T17:00:00"
    return p


def mark_rework(root, post_id, state):
    directory = root / "backups" / str(post_id)
    directory.mkdir(parents=True)
    (directory / f"editorial.{state}.json").write_text("{}")


def ids(client, cfg, root, count=2):
    return [card["id"] for card in build_cards(client, cfg, root, per_page=count)["cards"]]


def test_full_rework_cannot_starve_new(tmp_path, configure):
    posts = {i: make(i, "blocked" if i < 6 else "new") for i in range(1, 11)}
    for i in range(1, 6):
        mark_rework(tmp_path, i, "blocked")
    selected = ids(Client(posts), configure, tmp_path)
    assert len(selected) == 2
    assert any(i < 6 for i in selected)
    assert any(i >= 6 for i in selected)


def test_prepare_lock_is_session_local_and_does_not_consume_slot(tmp_path, configure, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "flow-lock-first")
    client = Client({1: make(1), 2: make(2)})
    with LockManager(tmp_path / "work" / "locks", ttl=900).acquire(1):
        first = prepare_batch(client, configure, tmp_path, [1, 2], batch_id="lock-first")
        assert [row["status"] for row in first["items"]] == ["lock_blocked", "prepared"]
        assert ids(client, configure, tmp_path) == [2]
    monkeypatch.setenv("HERMES_SESSION_ID", "flow-lock-next")
    assert 1 in ids(client, configure, tmp_path)


def test_generic_retry_is_deferred_across_sessions_without_wp_state_change(tmp_path, configure, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "flow-retry-first")
    client = Client({1: make(1), 2: make(2)})
    prepare_batch(client, configure, tmp_path, [1], batch_id="retry-first")
    original = dict(client.get_post(1)["meta"])
    envelope = {"schema_version": 1, "batch_id": "retry-first", "items": [{
        "post_id": 1, "status": "needs_retry", "reason": "retorno editorial incompleto",
    }]}
    result = _apply_editorial_batch(client, configure, tmp_path, envelope, compact=False)
    assert client.get_post(1)["meta"] == original
    assert result["posts"][0]["status"] == "needs_retry"
    monkeypatch.setenv("HERMES_SESSION_ID", "flow-retry-next")
    assert ids(client, configure, tmp_path) == [2]


def test_two_slot_rotation_across_cycles_and_empty_lane_redistribution(tmp_path, configure, monkeypatch):
    from unicornio_editor.retry_policy import record_v1_prepared

    client = Client({
        1: make(1, "partial"), 2: make(2, "blocked"), 3: make(3), 4: make(4, "partial"),
        5: make(5, "blocked"), 6: make(6), 7: make(7),
    })
    for cycle, expected in enumerate(([1, 3], [2, 6], [4, 7], [5, 3])):
        monkeypatch.setenv("HERMES_SESSION_ID", f"rotation-{cycle}")
        assert ids(client, configure, tmp_path) == expected
        # A repeated cards read is not served work and must not flip the lane.
        assert ids(client, configure, tmp_path) == expected
        chosen, fresh = expected
        # The partial receipt verification belongs to the existing resume
        # workflow; record a valid scheduling receipt after that boundary.
        record_v1_prepared(tmp_path, "partial" if chosen in (1, 4) else "recovery", chosen, f"turn-{cycle}")
        record_v1_prepared(tmp_path, "new", fresh, f"turn-{cycle}")
    client.posts = {3: client.posts[3], 6: client.posts[6]}
    assert set(ids(client, configure, tmp_path)) == {3, 6}


def test_real_preparation_advances_recovery_cursor_without_touching_wordpress(tmp_path, configure, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "one-card")
    client = Client({1: make(1, "blocked"), 2: make(2)})
    assert ids(client, configure, tmp_path, 1) == [1]
    result = prepare_batch(client, configure, tmp_path, [1], batch_id="rotation-blocked")
    assert result["prepared"] == 1
    assert client.posts[1]["meta"]["_hermes_state"] == "blocked"
    assert ids(client, configure, tmp_path, 1) == [2]


def test_needs_retry_does_not_mutate_ready_or_extend_stale_batch(tmp_path, configure, monkeypatch):
    from unicornio_editor.workflow import apply_editorial, build_queue_report

    monkeypatch.setenv("HERMES_SESSION_ID", "retry-old")
    client = Client({1: make(1), 2: make(2)})
    prepare_batch(client, configure, tmp_path, [1], batch_id="retry-older")
    prepare_batch(client, configure, tmp_path, [1], batch_id="retry-newer")
    row = {"post_id": 1, "status": "needs_retry", "reason": "ctx insuficiente"}
    old = {"schema_version": 1, "batch_id": "retry-older", "items": [row]}
    new = {**old, "batch_id": "retry-newer"}
    deferral = tmp_path / "backups/1/editorial.defer.json"
    result = _apply_editorial_batch(client, configure, tmp_path, old, compact=False)
    assert result["posts"][0]["stale"]
    assert not deferral.exists()
    result = _apply_editorial_batch(client, configure, tmp_path, new, compact=False)
    assert result["posts"][0]["not_before"]
    saved = deferral.read_bytes()
    _apply_editorial_batch(client, configure, tmp_path, new, compact=False)
    assert deferral.read_bytes() == saved
    assert build_queue_report(client, tmp_path)["unprocessed_ids"] == [2]
    client.posts[1]["meta"]["_hermes_state"] = "ready"
    initial = dict(client.posts[1]["meta"])
    result = _apply_editorial_batch(client, configure, tmp_path, old, compact=False)
    assert result["posts"][0]["status"] == "noop"
    unchanged = apply_editorial(client, configure, tmp_path, 1, {})
    assert unchanged["status"] == "noop"
    assert not unchanged["wordpress_changed"]
    assert client.posts[1]["meta"] == initial and deferral.read_bytes() == saved


def test_expired_or_changed_deferral_returns_to_monitor_and_cards(tmp_path, configure, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from unicornio_editor.retry_policy import record_v1_deferral
    from unicornio_editor.workflow import build_queue_report, _post_write_guard_signature

    client = Client({1: make(1), 2: make(2)})
    monkeypatch.setenv("HERMES_SESSION_ID", "new-cycle")
    current = datetime.now(timezone.utc)
    record_v1_deferral(tmp_path, client.posts[1], batch_id="one", signature=_post_write_guard_signature(client.posts[1]), reason_code="editorial_needs_retry",
                       delay_minutes=1, now=current - timedelta(minutes=2))
    record_v1_deferral(tmp_path, client.posts[2], batch_id="one", signature=_post_write_guard_signature(client.posts[2]), reason_code="editorial_needs_retry",
                       delay_minutes=5, now=current)
    assert ids(client, configure, tmp_path) == [1]
    assert build_queue_report(client, tmp_path)["unprocessed_ids"] == [1]
    client.posts[2]["title"]["raw"] = "Edited in the same second"
    assert ids(client, configure, tmp_path) == [1, 2]
    assert build_queue_report(client, tmp_path)["unprocessed_ids"] == [1, 2]


def test_wordpress_known_missing_card_refills_and_auth_errors_propagate(tmp_path, configure):
    from unicornio_editor.wordpress import WordPressError

    class HydrationClient(Client):
        failure = WordPressError("missing", status_code=404, api_code="rest_post_invalid_id")

        def list_pending_card_candidates(self, **options):
            return self.list_pending(**options)

        def get_post(self, post_id):
            if post_id == 1:
                raise self.failure
            return super().get_post(post_id)

    client = HydrationClient({1: make(1), 2: make(2), 3: make(3)})
    assert ids(client, configure, tmp_path) == [2, 3]
    for code in (401, 429, 503):
        client.failure = WordPressError("global", status_code=code)
        with pytest.raises(WordPressError):
            ids(client, configure, tmp_path)


def test_featured_known_local_miss_differs_from_global_failure(tmp_path, configure):
    from unicornio_editor.wordpress import WordPressError

    class FeaturedClient(Client):
        failure = WordPressError("missing", status_code=404, api_code="rest_post_invalid_id")

        def get_media(self, media_id):
            raise self.failure

    first, second = make(1), make(2)
    first["featured_media"] = 10
    client = FeaturedClient({1: first, 2: second})
    cards = build_cards(client, configure, tmp_path, per_page=2)["cards"]
    assert [c["id"] for c in cards] == [1, 2]
    assert cards[0]["featured"]["diagnostic_error_code"] == "media_not_found"
    for code in (401, 429, 503):
        client.failure = WordPressError("global", status_code=code)
        with pytest.raises(WordPressError):
            ids(client, configure, tmp_path)


def test_ownership_loss_after_work_started_is_not_retried_as_contention(tmp_path, configure, monkeypatch):
    from unicornio_editor import session_budget
    from unicornio_editor.locking import LockError

    monkeypatch.setenv("HERMES_SESSION_ID", "effect-uncertain")
    client = Client({1: make(1), 2: make(2)})
    monkeypatch.setattr("unicornio_editor.cli.preflight_apply_candidate", lambda *_: (True, ""))
    called = []

    def uncertain(*args, **kwargs):
        called.append(args[3])
        kwargs["on_work_started"]()
        raise LockError("ownership was lost at release")

    monkeypatch.setattr("unicornio_editor.cli.apply_editorial", uncertain)
    outcome = _apply_editorial_batch(client, configure, tmp_path, {
        "schema_version": 1, "batch_id": "unknown-effect",
        "items": [{"post_id": i, "editorial": {}} for i in (1, 2)],
    })
    assert outcome["posts"][0]["status"] == "integrity_blocked"
    assert called == [1]
    assert session_budget.skipped_locked_posts(tmp_path, configure) == set()
    assert session_budget.status(tmp_path, configure)["posts_touched"] == [1]


def test_mixed_batch_preserves_lock_and_retry_while_real_workflow_reaches_ready(tmp_path, configure, monkeypatch):
    import test_workflow as wf
    from unicornio_editor import session_budget

    class MixedClient(wf.FakeClient):
        def list_pending(self, *, page=1, per_page=20, include=None, status="pending"):
            rows = [p for p in self.posts if p["status"] == status and (include is None or p["id"] in include)]
            return rows[(page - 1) * per_page:page * per_page]

        def get_post(self, post_id):
            return next(p for p in self.posts if p["id"] == post_id)

        def update_post(self, post_id, payload):
            self.post = self.get_post(post_id)
            return super().update_post(post_id, payload)

    third = wf.WorkflowTests().post()
    third["id"] = 3
    client = MixedClient(third)
    client.posts = [make(1), make(2), third]
    monkeypatch.setenv("HERMES_SESSION_ID", "mixed-lock-retry-ready")
    with LockManager(tmp_path / "work" / "locks", ttl=900).acquire(1):
        prepared = prepare_batch(client, configure, tmp_path, [1, 2], batch_id="mixed")
        assert [row["status"] for row in prepared["items"]] == ["lock_blocked", "prepared"]
        batch = {"schema_version": 1, "batch_id": "mixed", "items": [
            {"post_id": 1, "editorial": wf.editorial_payload()},
            {"post_id": 2, "status": "needs_retry"},
            {"post_id": 3, "editorial": wf.editorial_payload()},
        ]}
        first = _apply_editorial_batch(client, configure, tmp_path, batch)
        assert [row["status"] for row in first["posts"]] == ["lock_blocked", "needs_retry", "ready"]
        assert session_budget.status(tmp_path, configure)["posts_touched"] == [3]
        ready_meta = dict(client.posts[2]["meta"])
        assert ready_meta["_hermes_ready_hash"] and ready_meta["_hermes_ready_manifest"]
        editorial_file = tmp_path / "backups/3/editorial.latest.json"
        saved_editorial = editorial_file.read_bytes()
        assert all(post_id == 3 for post_id, _payload in client.updated)
        second = _apply_editorial_batch(client, configure, tmp_path, batch)
        assert second["posts"][-1]["status"] == "noop"
        assert len(client.updated) == 1
        assert client.posts[2]["meta"] == ready_meta
        assert editorial_file.read_bytes() == saved_editorial
    monkeypatch.setenv("HERMES_SESSION_ID", "mixed-next-cycle")
    assert ids(client, configure, tmp_path) == [1]


def test_verified_global_429_does_not_become_per_post_cooldown(tmp_path, configure):
    from unicornio_editor.retry_policy import editorial_backoff_status, record_editorial_backoff

    client = Client({1: make(1)})
    prepare_batch(client, configure, tmp_path, [1], batch_id="global-backoff")
    marker = record_editorial_backoff(
        tmp_path, batch_id="global-backoff", http_status=429,
        retry_after_seconds=600, failure_class="rate_limited",
    )
    result = _apply_editorial_batch(client, configure, tmp_path, {
        "schema_version": 1, "batch_id": "global-backoff", "items": [{
            "post_id": 1, "status": "needs_retry", "failure_class": "rate_limited", "http_status": 429,
        }],
    })
    assert result["posts"][0]["status"] == "needs_retry"
    assert not (tmp_path / "backups/1/editorial.defer.json").exists()
    assert editorial_backoff_status(tmp_path)["not_before"] == marker["not_before"]


def test_vision_provider_outage_uses_operational_cooldown_without_editorial_penalty(tmp_path, configure, monkeypatch):
    from datetime import datetime, timezone

    import test_workflow as wf
    from unicornio_editor.state import read_state
    from unicornio_editor.workflow import apply_editorial

    client = wf.FakeClient(wf.WorkflowTests().post())
    editorial = wf.editorial_payload()
    editorial["cleaned_html"] = "<p>Texto revisado sobre videogame e lançamento.</p>"
    monkeypatch.setattr("unicornio_editor.workflow._execute_media_plan", lambda *_a, **_kw: (
        [{"status": "deferred", "reason_code": "vision_provider_error", "recoverable": True}], None, None,
    ))
    before = datetime.now(timezone.utc)
    result = apply_editorial(client, configure, tmp_path, 42, editorial)
    assert result["status"] == "partial"
    state = read_state(client.get_post(42))
    assert state["no_progress_attempts"] == 0 and state["attempts"] == 0
    assert (datetime.fromisoformat(state["next_retry_at"]) - before).total_seconds() >= 7198


def test_claimed_429_without_provider_marker_does_not_bypass_post_cooldown(tmp_path, configure):
    client = Client({1: make(1)})
    prepare_batch(client, configure, tmp_path, [1], batch_id="unverified-backoff")
    result = _apply_editorial_batch(client, configure, tmp_path, {
        "schema_version": 1, "batch_id": "unverified-backoff", "items": [{
            "post_id": 1, "status": "needs_retry", "failure_class": "rate_limited", "http_status": 429,
        }],
    })
    assert result["posts"][0]["error_code"] == "editorial_needs_retry"
    assert result["posts"][0]["not_before"]


def test_legacy_unverifiable_lock_is_skipped_without_unlock_or_rewrite(tmp_path, configure, monkeypatch):
    import json
    from unicornio_editor.locking import LockManager, classify_lock_owner_record

    monkeypatch.setenv("HERMES_SESSION_ID", "legacy-lock")
    lock_dir = tmp_path / "work/locks"
    lock_dir.mkdir(parents=True)
    path = lock_dir / "115866.lock"
    legacy = b'{"token":"old-owner-with-no-process","released":false}'
    path.write_bytes(legacy)
    assert classify_lock_owner_record(json.loads(legacy))[0] == "legacy_unverifiable"
    inode = path.stat().st_ino
    client = Client({115866: make(115866), 115867: make(115867)})
    # prepare has the real LockManager acquisition path and no synthetic error.
    assert LockManager(lock_dir, ttl=900)
    first = prepare_batch(client, configure, tmp_path, [115866, 115867], batch_id="legacy-lock-first")
    assert [r["status"] for r in first["items"]] == ["lock_blocked", "prepared"]
    assert ids(client, configure, tmp_path) == [115867]
    replay = prepare_batch(client, configure, tmp_path, [115866], batch_id="legacy-lock-replay")
    assert replay["items"][0]["session_skipped"] is True
    assert path.read_bytes() == legacy and path.stat().st_ino == inode
    monkeypatch.setenv("HERMES_SESSION_ID", "legacy-lock-following")
    assert 115866 in ids(client, configure, tmp_path)
    assert path.read_bytes() == legacy


def test_largely_new_backlog_and_saturated_known_rework_stop_with_bounded_reads(tmp_path, configure):
    class CountingClient(Client):
        calls = []

        def list_pending_card_candidates(self, **options):
            self.calls.append(options)
            return self.list_pending(**options)

    client = CountingClient({i: make(i) for i in range(1, 6001)})
    report = build_cards(client, configure, tmp_path, per_page=2)
    assert report["count"] == 2 and report["has_more_pending"]
    assert len(client.calls) == 2 and all(not call.get("include") for call in client.calls)
    for i in range(1, 6):
        client.posts[i]["meta"]["_hermes_state"] = "blocked"
        mark_rework(tmp_path, i, "blocked")
    client.calls.clear()
    report = build_cards(client, configure, tmp_path, per_page=2)
    assert [(c["id"], c["state"]) for c in report["cards"]] == [(1, "blocked"), (6, "new")]
    assert len(client.calls) == 2
    assert client.calls[0]["include"] == [1, 2, 3, 4, 5] and not client.calls[1].get("include")
    assert report["has_more_pending"] and not report["pending_scan_complete"]


@pytest.mark.parametrize("fallback_count", [2, 3])
def test_sparse_adequate_fallback_avoids_terminal_backlog_sweep(tmp_path, configure, fallback_count):
    class CountingClient(Client):
        def __init__(self, posts):
            super().__init__(posts)
            self.pages = []

        def list_pending_card_candidates(self, **options):
            if options.get("include") is None:
                self.pages.append(options["page"])
            return self.list_pending(**options)

    client = CountingClient({i: make(i, "blocked" if i <= fallback_count else "ready")
                             for i in range(1, 10001)})
    for i in range(1, fallback_count + 1):
        mark_rework(tmp_path, i, "blocked")
    report = build_cards(client, configure, tmp_path, per_page=2)
    assert [card["id"] for card in report["cards"]] == [1, 2]
    assert client.pages == [1, 2]
    assert report["has_more_pending"] and not report["pending_scan_complete"]


def test_new_to_ready_during_hydration_refills_reserved_new_slot(tmp_path, configure):
    from copy import deepcopy

    class HydrationClient(Client):
        def __init__(self, posts):
            super().__init__(posts)
            self.hydrated = []

        def list_pending_card_candidates(self, **options):
            return self.list_pending(**options)

        def get_post(self, post_id):
            self.hydrated.append(post_id)
            post = deepcopy(super().get_post(post_id))
            if post_id == 3:
                post["meta"]["_hermes_state"] = "ready"
            return post

    client = HydrationClient({1: make(1, "partial"), 2: make(2, "blocked"), 3: make(3), 4: make(4)})
    mark_rework(tmp_path, 1, "partial")
    mark_rework(tmp_path, 2, "blocked")
    assert ids(client, configure, tmp_path) == [1, 4]
    assert client.hydrated == [1, 3, 4]  # Recovery may not take the reserved NEW slot.


def test_unavailable_rework_does_not_satisfy_fallback_ahead_of_new(tmp_path, configure):
    import json

    class CountingClient(Client):
        def __init__(self, posts):
            super().__init__(posts)
            self.pages = []

        def list_pending_card_candidates(self, **options):
            if options.get("include") is None:
                self.pages.append(options["page"])
            return self.list_pending(**options)

    policy = tmp_path / "config/v2-quarantine.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({
        "version": 1, "release_history": [],
        "posts": [{"post_id": 1, "reason_code": "integrity", "reason": "test fixture"}],
    }))
    posts = {i: make(i, "ready") for i in range(1, 306)}
    posts.update({1: make(1, "blocked"), 2: make(2, "partial"), 3: make(3, "blocked"), 301: make(301)})
    posts[2]["meta"]["_hermes_next_retry_at"] = "2099-01-01T00:00:00Z"
    for post_id, state in [(1, "blocked"), (2, "partial"), (3, "blocked"), (4, "blocked")]:
        mark_rework(tmp_path, post_id, state)
    client = CountingClient(posts)
    assert ids(client, configure, tmp_path) == [3, 301]
    assert client.pages == [1, 2, 3, 4]


def test_missing_or_same_second_revision_cannot_veto_even_lightweight_cards(tmp_path, configure):
    from unicornio_editor.retry_policy import record_v1_deferral
    from unicornio_editor.workflow import _post_write_guard_signature, build_queue_report

    class LightweightClient(Client):
        def list_pending_card_candidates(self, **kwargs):
            return [
                {k: row[k] for k in ("id", "status", "modified_gmt", "meta") if k in row}
                for row in self.list_pending(**kwargs)
            ]

    client = LightweightClient({1: make(1), 2: make(2)})
    post = client.posts[1]
    record_v1_deferral(tmp_path, post, batch_id="exact-old", signature=_post_write_guard_signature(post),
                       reason_code="editorial_needs_retry", delay_minutes=10)
    assert ids(client, configure, tmp_path) == [2]
    assert build_queue_report(client, tmp_path)["unprocessed_ids"] == [2]
    post["content"]["raw"] += "<p>Mudança na mesma fração.</p>"
    assert ids(client, configure, tmp_path) == [1, 2]
    assert build_queue_report(client, tmp_path)["unprocessed_ids"] == [1, 2]
    assert prepare_batch(client, configure, tmp_path, [1], batch_id="exact-newer")["prepared"] == 1
    post.pop("modified_gmt")
    assert 1 in ids(client, configure, tmp_path)
    assert 1 in build_queue_report(client, tmp_path)["unprocessed_ids"]


def test_epoch_protected_candidate_never_counts_as_available_or_prepared(tmp_path, configure):
    import json

    from unicornio_editor.retry_policy import v1_rotation
    from unicornio_editor.workflow import build_queue_report

    epoch = tmp_path / "work/v2-epochs/active/epoch.json"
    epoch.parent.mkdir(parents=True)
    epoch.write_text(json.dumps({"schema_version": 1, "epoch_id": "test", "protected_transaction_ids": [1]}))
    client = Client({1: make(1), 2: make(2)})
    assert ids(client, configure, tmp_path) == [2]
    assert build_queue_report(client, tmp_path)["unprocessed_ids"] == [2]
    out = prepare_batch(client, configure, tmp_path, [1], batch_id="protected-post")
    assert out["items"][0]["error_code"] == "epoch_protected_transaction"
    assert not (tmp_path / "backups/1/prepared.json").exists()
    assert v1_rotation(tmp_path) == {}


def test_wrong_wordpress_post_identity_stops_prepare_and_apply(tmp_path, configure, monkeypatch):
    from unicornio_editor import session_budget
    from unicornio_editor.wordpress import WordPressError

    class WrongIdentity(Client):
        def get_post(self, post_id):
            return self.posts[2]

    client = WrongIdentity({1: make(1), 2: make(2)})
    with pytest.raises(WordPressError) as exc:
        prepare_batch(client, configure, tmp_path, [1, 2], batch_id="unsafe-identity")
    assert exc.value.error_code == "wordpress_integrity"
    apply = _apply_editorial_batch(client, configure, tmp_path, {
        "schema_version": 1, "batch_id": "unsafe-identity", "items": [
            {"post_id": 1, "editorial": {}}, {"post_id": 2, "editorial": {}},
        ],
    })
    assert apply["stopped"] == "wordpress_integrity" and apply["remaining_post_ids"] == [1, 2]
    assert session_budget.status(tmp_path, configure)["posts_touched"] == []
