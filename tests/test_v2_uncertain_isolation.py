import json
import sqlite3
from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2.cycle_admin import (
    TransactionIsolationError,
    classify_transaction,
    isolate_uncertain_transaction,
)
from unicornio_editor.pipeline_v2.cycle_state import CycleState, CycleStateStore
from unicornio_editor.workflow import publish_ready_posts

CYCLE = "4a2256fc0b744d45b8125139c197fee6"
AUTH = "a4371791eb824734b4b4c88ae0334a71"
RUN = "5e42e98f2d81423f90a66e8adbbaa8b8"
SELECTED = [115524, 115331, 115215, 115512, 115338]
POST = 115331


class FakeClient:
    def __init__(self, post=None):
        self.post = post or {
            "id": POST,
            "status": "awaiting_human",
            "featured_media": 115332,
            "meta": {
                "_hermes_work_state": json.dumps({
                    "version": 2,
                    "state": "human_required",
                    "phase": "media",
                    "blocker": "inline_missing",
                }),
            },
        }
        self.get_calls = 0

    def get_post(self, post_id):
        assert int(post_id) == POST
        self.get_calls += 1
        return json.loads(json.dumps(self.post))

    def list_pending(self, *, page, per_page):
        return []



def test_transaction_classification_contracts():
    identity = {"run_id": "r", "cycle_id": "c", "cycle_round": 1, "authorization_id": "a", "post_id": 1}
    assert classify_transaction(None, external_evidence={"effects_absent": True}) == "NOT_EXECUTED"
    assert classify_transaction(None) == "UNCERTAIN_ISOLATED"
    committed = {"status": "committed", "readback": True, **identity}
    assert classify_transaction(committed) == "UNCERTAIN_ISOLATED"
    assert classify_transaction(committed, external_evidence={**identity, "readback_confirmed": True}) == "COMMITTED_NO_REPLAY"
    prewriter = {"status": "failed_pre_writer", "writer_started": False, "resumable": True, **identity}
    assert classify_transaction(prewriter) == "UNCERTAIN_ISOLATED"
    assert classify_transaction(prewriter, external_evidence={**identity, "effects_absent": True}) == "PRE_WRITER_RECOVERABLE"
    assert classify_transaction({"status": "committing", "readback": None}) == "UNCERTAIN_ISOLATED"

def _fixture(root: Path):
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )
    store = CycleStateStore(root)
    state = CycleState(cycle_id=CYCLE, phase="ADMISSION", status="running", config={"ready_target": 5})
    state.pending_round = {
        "round_number": 1,
        "authorization_id": AUTH,
        "preview_selected_ids": SELECTED,
        "request": {"batch_size": 5},
    }
    store.save(state, event="round_authorized", round_number=1, authorization_id=AUTH, selected_ids=SELECTED)
    journal_dir = root / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / f"{POST}.json").write_text(json.dumps({
        "status": "committing",
        "post_id": POST,
        "run_id": RUN,
        "cycle_id": CYCLE,
        "cycle_round": 1,
        "authorization_id": AUTH,
        "state": {"version": 2, "state": "human_required", "phase": "media", "blocker": "inline_missing"},
        "media_state": "apply_not_attempted",
        "media_readback_status": "not_attempted",
        "media_ids": [],
        "apply_media_records": [],
    }), encoding="utf-8")
    db = root / "work" / "media_registry.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE media_runs (run_id TEXT, post_id INTEGER, acquisition_outcome TEXT, created_at TEXT);
            CREATE TABLE media_decisions (run_id TEXT, post_id INTEGER, disposition TEXT, reason_code TEXT);
            CREATE TABLE media_usage (post_id INTEGER, status TEXT, applied_at TEXT);
            """
        )
        con.execute("INSERT INTO media_runs VALUES (?, ?, ?, ?)", (RUN, POST, "started", "2026-10-23T04:03:17Z"))
        con.execute("INSERT INTO media_decisions VALUES (?, ?, ?, ?)", (RUN, POST, "started", "media"))


def _args(root):
    return dict(
        root=root,
        client=FakeClient(),
        cycle_id=CYCLE,
        authorization_id=AUTH,
        selected_ids=SELECTED,
        pending_round=1,
        post_id=POST,
        run_id=RUN,
    )


def test_preview_classifies_uncertain_without_writing(tmp_path):
    _fixture(tmp_path)
    result = isolate_uncertain_transaction(**_args(tmp_path), apply=False)
    assert result["status"] == "preview"
    assert result["classification"] == "UNCERTAIN_ISOLATED"
    assert result["wordpress_writes"] == 0
    assert not (tmp_path / "work" / "v2-cycle" / "transaction-isolations").exists()
    assert CycleStateStore(tmp_path).load().pending_round["preview_selected_ids"] == SELECTED


def test_apply_requires_exact_preview_and_operator_confirmation(tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    with pytest.raises(TransactionIsolationError, match="operator"):
        isolate_uncertain_transaction(**args, apply=True, preview=preview)
    changed = dict(preview)
    changed["snapshot_hash"] = "different"
    with pytest.raises(TransactionIsolationError, match="changed"):
        isolate_uncertain_transaction(**args, apply=True, preview=changed, operator_authorization="joao")


def test_apply_isolates_post_preserves_journal_and_releases_other_selection(tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    result = isolate_uncertain_transaction(
        **args,
        apply=True,
        preview=preview,
        operator_authorization="joao-explicit-authorization",
    )
    assert result["status"] == "applied"
    assert result["classification"] == "UNCERTAIN_ISOLATED"
    state = CycleStateStore(tmp_path).load()
    assert state.pending_round is None
    assert state.status == "running"
    original = json.loads((tmp_path / "work" / "v2-journal" / f"{POST}.json").read_text())
    assert original["status"] == "committing"
    policy = json.loads((tmp_path / "config" / "v2-quarantine.json").read_text())
    assert [row["post_id"] for row in policy["posts"]] == [POST]
    assert policy["posts"][0]["reason_code"] == "uncertain_transaction"
    assert result["wordpress_writes"] == 0
    assert any(
        line.get("event") == "transaction_uncertain_isolated"
        for line in map(json.loads, (tmp_path / "work/v2-cycle/journal.jsonl").read_text().splitlines())
    )
    archive = Path(result["archive_dir"])
    assert json.loads((archive / f"journal-{POST}.json").read_text())["status"] == "committing"
    assert (archive / "isolation-record.json").is_file()


def test_apply_is_idempotent_and_does_not_duplicate_event(tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    first = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    second = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    assert second["status"] == "already_applied"
    lines = (tmp_path / "work/v2-cycle/journal.jsonl").read_text().splitlines()
    assert sum(json.loads(line).get("event") == "transaction_uncertain_isolated" for line in lines) == 1
    assert second["archive_dir"] == first["archive_dir"]


def test_crash_after_quarantine_resumes_without_replay(monkeypatch, tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_save = CycleStateStore.save

    def crash_before_checkpoint(self, *a, **kw):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(CycleStateStore, "save", crash_before_checkpoint)
    with pytest.raises(RuntimeError, match="simulated crash"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    monkeypatch.setattr(CycleStateStore, "save", original_save)
    transition = next((tmp_path / "work/v2-cycle/transaction-isolations").glob("*.json"))
    assert json.loads(transition.read_text())["phase"] == "quarantine_applied"
    resumed = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    assert resumed["status"] == "applied"
    assert CycleStateStore(tmp_path).load().pending_round is None
    assert json.loads((tmp_path / "work/v2-journal" / f"{POST}.json").read_text())["status"] == "committing"


def test_identity_divergence_and_post_change_abort_before_apply(tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    bad = dict(args, run_id="other-run")
    with pytest.raises(TransactionIsolationError, match="run_id"):
        isolate_uncertain_transaction(**bad, apply=False)
    args["client"].post["modified"] = "human-changed"
    with pytest.raises(TransactionIsolationError, match="changed"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )


def test_publisher_skips_quarantined_ready_post(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [{"post_id": POST, "reason_code": "uncertain_transaction", "reason": "hold"}], "release_history": []}),
        encoding="utf-8",
    )
    called = []
    monkeypatch.setattr("unicornio_editor.workflow._reconcile_persisted_uncertain_publications", lambda *a: [])
    monkeypatch.setattr("unicornio_editor.workflow.publish_post", lambda *a: called.append(a) or {"status": "published"})
    client = FakeClient({
        "id": POST,
        "status": "pending",
        "meta": {
            "_hermes_work_state": json.dumps({"version": 2, "state": "ready", "phase": "validate"}),
            "_hermes_ready_hash": "hash",
        },
    })
    config = type("Config", (), {"dry_run": False})()
    publish_ready_posts(client, config, tmp_path)
    assert called == []
