import json
from types import SimpleNamespace

from unicornio_editor.pipeline_v2.model import LifecycleState, Phase, WorkState
from unicornio_editor.pipeline_v2.runtime import run_v2


class Client:
    def __init__(self, posts):
        self.posts = {post["id"]: post for post in posts}
        self.updates = []

    def list_pending(self, *, page, per_page, status):
        return list(self.posts.values()) if page == 1 else []

    def get_post(self, post_id):
        return self.posts[post_id]

    def update_post(self, post_id, payload):
        self.updates.append((post_id, payload))


def post(post_id, date, state):
    return {
        "id": post_id,
        "status": "pending",
        "date_gmt": date,
        "title": {"raw": f"Post {post_id}"},
        "content": {"raw": "<p>Texto editorial em português.</p>"},
        "meta": {"_hermes_work_state": json.dumps(state.to_dict())},
    }


def config(cohort_path):
    return SimpleNamespace(
        v2_historical_cohort_file=cohort_path,
        v2_admission_after="2026-10-09T00:00:00Z",
        v2_admission_allowlist=(),
    )




def test_preview_requeues_human_required_when_human_moves_wp_status_to_pending(tmp_path):
    state = WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA)
    reopened = post(3, "2026-10-09T01:00:00Z", state)
    reopened["meta"]["_hermes_work_state"] = json.dumps(state.to_dict())
    reopened["meta"]["_hermes_human_reopened_at"] = "2030-01-01T00:00:00+00:00"
    client = Client([reopened])

    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"version": 1, "posts": []}), encoding="utf-8")
    result = run_v2(client, config(cohort), tmp_path, post_id=3, preview=True)

    assert result["selected"] == 1
    assert result["queue"]["requested_pending"] is True
    assert result["queue"]["requested_eligible"] is True
    assert result["queue"]["selected_ids"] == [3]




def test_preview_does_not_requeue_human_required_without_manual_reopen_marker(tmp_path):
    state = WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.MEDIA)
    reopened = post(4, "2026-10-09T01:00:00Z", state)
    client = Client([reopened])
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({"version": 1, "posts": []}), encoding="utf-8")

    result = run_v2(client, config(cohort), tmp_path, post_id=4, preview=True)

    assert result["selected"] == 0
    assert result["queue"]["requested_eligible"] is False


def test_preview_post_id_uses_admission_and_never_falls_back(tmp_path):
    cohort = tmp_path / "cohort.json"
    cohort.write_text(json.dumps({
        "version": 1,
        "posts": [{
            "post_id": 1,
            "original_datetime": "2026-10-01T00:00:00Z",
            "classification": "historical",
        }],
    }), encoding="utf-8")
    state = WorkState(state=LifecycleState.PENDING, phase=Phase.RELEVANCE)
    client = Client([
        post(1, "2026-10-01T00:00:00Z", state),
        post(2, "2026-10-09T01:00:00Z", state),
    ])

    excluded = run_v2(client, config(cohort), tmp_path, post_id=1, preview=True)
    assert excluded["preview"] is True
    assert excluded["selected"] == 0
    assert excluded["queue"]["request_reason"] == "not_admitted"
    assert client.updates == []

    selected = run_v2(client, config(cohort), tmp_path, post_id=2, preview=True)
    assert selected["selected"] == 1
    assert selected["queue"]["selected_ids"] == [2]
    assert selected["details"][0]["post_id"] == 2
    assert client.updates == []
