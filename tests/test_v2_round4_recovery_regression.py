import json
import sqlite3

from unicornio_editor.pipeline_v2 import cycle_runtime
from unicornio_editor.pipeline_v2.model import LifecycleState, Phase, WorkState


CYCLE_ID = "7f401e9af11145c38573482bc5a3026d"
AUTHORIZATION_ID = "71dd4c5c82fa41b8b8e6d7061d75a38b"
SELECTED = [115592, 115629, 115282, 115631, 115530]


def _state(state, phase):
    return WorkState(
        state=state,
        phase=phase,
        relevance_approved=True,
    )


def test_round4_mixed_journals_reconcile_without_replay_or_fake_correlation(tmp_path):
    journal_dir = tmp_path / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    with sqlite3.connect(tmp_path / "work" / "media_registry.sqlite") as db:
        db.executescript(
            """
            CREATE TABLE media_runs (
                run_id TEXT, post_id INTEGER, planned_deficits TEXT,
                acquisition_outcome TEXT, created_at TEXT
            );
            CREATE TABLE media_decisions (
                run_id TEXT, post_id INTEGER, candidate_id TEXT,
                disposition TEXT, reason_code TEXT, scope TEXT, created_at TEXT
            );
            CREATE TABLE media_usage (
                post_id INTEGER, wordpress_media_id INTEGER,
                status TEXT, applied_at TEXT
            );
            """
        )
    posts = {}

    # Current authorization: committed evidence is reconciled, never replayed.
    for post_id, state, phase in (
        (115592, LifecycleState.READY, Phase.VALIDATE),
        (115629, LifecycleState.PENDING, Phase.MEDIA),
    ):
        posts[post_id] = {
            "id": post_id,
            "status": "pending",
            "title": {"raw": f"post {post_id}"},
            "content": {"raw": "<p>fixture</p>"},
            "meta": {"_hermes_work_state": json.dumps(_state(state, phase).to_dict())},
        }
        (journal_dir / f"{post_id}.json").write_text(
            json.dumps(
                {
                    "status": "committed",
                    "readback": True,
                    "cycle_id": CYCLE_ID,
                    "cycle_round": 4,
                    "authorization_id": AUTHORIZATION_ID,
                    "state": _state(state, phase).to_dict(),
                    "media_readback_status": "confirmed" if post_id == 115629 else "not_attempted",
                }
            ),
            encoding="utf-8",
        )

    # Historical journals must not be counted for round 4.
    for post_id, old_cycle, old_round in ((115631, "old-cycle", 3), (115530, "old-cycle", 1)):
        posts[post_id] = {
            "id": post_id,
            "status": "pending",
            "title": {"raw": f"post {post_id}"},
            "content": {"raw": "<p>fixture</p>"},
            "meta": {"_hermes_work_state": json.dumps(_state(LifecycleState.PENDING, Phase.RELEVANCE).to_dict())},
        }
        (journal_dir / f"{post_id}.json").write_text(
            json.dumps(
                {
                    "status": "committed",
                    "readback": True,
                    "cycle_id": old_cycle,
                    "cycle_round": old_round,
                    "authorization_id": f"historical-{old_round}",
                    "state": _state(LifecycleState.PENDING, Phase.RELEVANCE).to_dict(),
                }
            ),
            encoding="utf-8",
        )

    # 115282 is the unresolved running journal: no retroactive identity is added.
    posts[115282] = {
        "id": 115282,
        "status": "pending",
        "title": {"raw": "post 115282"},
        "content": {"raw": "<p>fixture</p>"},
        "meta": {"_hermes_work_state": json.dumps(_state(LifecycleState.PENDING, Phase.MEDIA).to_dict())},
    }
    (journal_dir / "115282.json").write_text(
        json.dumps({"status": "running", "run_id": "8e3c3735e2064c8eb0de1b204f47a694"}),
        encoding="utf-8",
    )

    class ReadOnlyClient:
        def __init__(self):
            self.get_calls = []
            self.write_calls = []

        def list_pending(self, *, page, per_page, status):
            return list(posts.values()) if page == 1 else []

        def get_post(self, post_id):
            self.get_calls.append(post_id)
            return posts[post_id]

        def update_post(self, *args, **kwargs):
            self.write_calls.append((args, kwargs))
            raise AssertionError("recovery fixture attempted a WordPress write")

        def upload_media(self, *args, **kwargs):
            self.write_calls.append((args, kwargs))
            raise AssertionError("recovery fixture attempted a media write")

    client = ReadOnlyClient()
    result = cycle_runtime._reconcile_completed_round(
        client,
        tmp_path,
        SELECTED,
        expected_cycle_id=CYCLE_ID,
        expected_cycle_round=4,
        expected_authorization_id=AUTHORIZATION_ID,
    )

    assert result is not None
    assert result.reconciled_without_replay is False
    assert result.reconciliation_blocked is True
    assert result.executed_ids == (115592, 115629)
    by_id = {detail["post_id"]: detail for detail in result.details}
    assert by_id[115592]["outcome"] == "ready"
    assert by_id[115629]["outcome"] == "retry"
    assert by_id[115631]["outcome"] == "not_executed"
    assert by_id[115530]["outcome"] == "not_executed"
    assert result.reconciliation_blocking_info is not None
    assert any(item["post_id"] == 115282 for item in result.reconciliation_blocking_info["unresolved_posts"])
    assert client.write_calls == []
    assert "authorization_id" not in json.loads((journal_dir / "115282.json").read_text())
