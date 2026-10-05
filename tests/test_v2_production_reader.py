from datetime import datetime, timezone

from unicornio_editor.pipeline_v2.model import LifecycleState, Phase, RetryInfo, WorkState
from unicornio_editor.pipeline_v2.production import ProductionCandidateReader


def test_candidate_reader_paginates_deduplicates_and_skips_terminal(tmp_path):
    posts = [
        {"id": 1, "status": "pending", "title": {"raw": "new"}, "content": {"raw": "<p>x</p>"}, "meta": {}},
        {"id": 2, "status": "pending", "title": {"raw": "ready"}, "content": {"raw": "<p>x</p>"}, "meta": {"_hermes_work_state": '{"version":2,"state":"ready","phase":"validate","blocker":null,"retry":{"attempts":0,"no_progress":0,"next_at":null},"editorial":{"relevance_approved":true},"media":{"inline":{"required":0,"accepted":[],"accepted_count":0,"missing":0},"featured":{"status":"missing","media_id":null,"media_url":null}}}'}},
        {"id": 1, "status": "pending", "title": {"raw": "duplicate"}, "content": {"raw": "<p>x</p>"}, "meta": {}},
    ]

    class Client:
        def list_pending(self, *, page, per_page, status="pending"):
            return posts if page == 1 else []

        def get_post(self, post_id):
            return next(post for post in posts if post["id"] == post_id)

    candidates = ProductionCandidateReader(Client(), tmp_path, now=datetime(2026, 10, 5, tzinfo=timezone.utc)).read(page_size=100)
    assert [post_id for post_id, _ in candidates] == [1]
    assert candidates[0][1]["editorial"] is None
    assert candidates[0][1]["v2_state"].state is LifecycleState.PENDING
