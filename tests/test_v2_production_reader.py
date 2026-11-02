import json
from datetime import datetime, timezone

from unicornio_editor.pipeline_v2.model import BlockerCode, LifecycleState, Phase, RetryInfo, WorkState
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


def test_candidate_reader_clamps_wordpress_page_size(tmp_path):
    calls = []
    post = {"id": 3, "status": "pending", "title": {"raw": "new"}, "content": {"raw": "x"}, "meta": {}}

    class Client:
        def list_pending(self, *, page, per_page, status="pending"):
            calls.append(per_page)
            assert 1 <= per_page <= 100
            return [post] if page == 1 else []

        def get_post(self, post_id):
            return post

    ProductionCandidateReader(Client(), tmp_path).snapshot(page_size=1000)
    assert calls == [100]


def test_candidate_reader_reenters_editorial_for_legacy_draft_without_relevance(tmp_path):
    body = "<p>" + ("Texto editorial recuperado com contexto suficiente para continuar. " * 8) + "</p>"
    post = {
        "id": 115900,
        "status": "pending",
        "title": {"raw": "Notícia de videogame"},
        "content": {"raw": ""},
        "meta": {
            "_hermes_work_state": json.dumps(
                WorkState(
                    phase=Phase.MEDIA,
                    blocker=BlockerCode.INLINE_MISSING,
                    relevance_approved=True,
                ).to_dict()
            )
        },
    }

    class Client:
        def list_pending(self, *, page, per_page, status="pending"):
            return [post] if page == 1 else []

        def get_post(self, post_id):
            return post

    draft = tmp_path / "backups" / "115900" / "editorial.draft.json"
    draft.parent.mkdir(parents=True)
    draft.write_text(json.dumps({"cleaned_html": body, "decision": "process"}), encoding="utf-8")

    candidates = ProductionCandidateReader(Client(), tmp_path).snapshot(page_size=100)

    recovered = candidates[0][1]["v2_state"]
    assert recovered.state is LifecycleState.PENDING
    assert recovered.phase is Phase.EDITORIAL
    assert recovered.blocker is BlockerCode.SCHEMA
    assert recovered.relevance_approved is False


def test_candidate_reader_keeps_later_phase_for_valid_relevance_contract(tmp_path):
    body = "<p>" + ("Texto editorial recuperado com contexto suficiente para continuar. " * 8) + "</p>"
    post = {
        "id": 115901,
        "status": "pending",
        "title": {"raw": "Notícia de videogame"},
        "content": {"raw": ""},
        "meta": {
            "_hermes_work_state": json.dumps(
                WorkState(
                    phase=Phase.MEDIA,
                    blocker=BlockerCode.INLINE_MISSING,
                    relevance_approved=True,
                ).to_dict()
            )
        },
    }

    class Client:
        def list_pending(self, *, page, per_page, status="pending"):
            return [post] if page == 1 else []

        def get_post(self, post_id):
            return post

    draft = tmp_path / "backups" / "115901" / "editorial.draft.json"
    draft.parent.mkdir(parents=True)
    draft.write_text(
        json.dumps(
            {
                "cleaned_html": body,
                "site_relevance": {
                    "decision": "process",
                    "confidence": 0.95,
                    "reason": "assunto compatível",
                    "matched_topics": ["games"],
                },
            }
        ),
        encoding="utf-8",
    )

    candidates = ProductionCandidateReader(Client(), tmp_path).snapshot(page_size=100)

    recovered = candidates[0][1]["v2_state"]
    assert recovered.phase is Phase.MEDIA
    assert recovered.blocker is BlockerCode.INLINE_MISSING


def test_corrupt_candidate_artifact_is_isolated_and_not_reintroduced_as_new(tmp_path):
    posts = [
        {"id": 10, "status": "pending", "title": {"raw": "bad"}, "content": {"raw": "x"}, "meta": {}},
        {"id": 11, "status": "pending", "title": {"raw": "good"}, "content": {"raw": "y"}, "meta": {}},
    ]

    class Client:
        def list_pending(self, *, page, per_page, status="pending"):
            return posts if page == 1 else []

        def get_post(self, post_id):
            return next(post for post in posts if post["id"] == post_id)

    bad_artifact = tmp_path / "backups" / "10" / "editorial.draft.json"
    bad_artifact.parent.mkdir(parents=True)
    bad_artifact.write_text("{invalid", encoding="utf-8")

    reader = ProductionCandidateReader(Client(), tmp_path)
    candidates = reader.read(page_size=100)

    assert [post_id for post_id, _ in candidates] == [11]
    assert reader.load_errors == [{
        "post_id": 10,
        "error_class": "BatchError",
        "detail": reader.load_errors[0]["detail"],
    }]
    assert "JSON invalido" in reader.load_errors[0]["detail"]


def test_candidate_reader_reports_hard_page_cap_instead_of_silent_partial_queue(tmp_path):
    class Client:
        def list_pending(self, *, page, per_page, status="pending"):
            if page <= 2:
                return [{"id": page, "status": "pending", "title": {"raw": "t"}, "content": {"raw": "x"}, "meta": {}}]
            return []

        def get_post(self, post_id):
            return {
                "id": post_id,
                "status": "pending",
                "title": {"raw": "t"},
                "content": {"raw": "x"},
                "meta": {},
            }

    reader = ProductionCandidateReader(Client(), tmp_path)
    try:
        reader.snapshot(page_size=1, max_pages=1)
    except Exception as exc:
        assert "resume at page 2" in str(exc)
    else:
        raise AssertionError("truncated queue must not be returned as complete")
    assert reader.pagination == {"truncated": True, "next_page": 2}
