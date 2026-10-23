import json
import sqlite3
from pathlib import Path

import pytest

import unicornio_editor.pipeline_v2.transaction_admin as transaction_admin
from unicornio_editor.pipeline_v2.cycle_admin import (
    TransactionIsolationError,
    classify_transaction,
    isolate_uncertain_transaction,
)
from unicornio_editor.pipeline_v2.cycle_state import CycleState, CycleStateStore
from unicornio_editor.workflow import publish_ready_posts
from unicornio_editor.backup import atomic_write_text

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
        self.posts = {}
        self.get_calls = 0
        self.write_calls = 0

    def get_post(self, post_id):
        self.get_calls += 1
        if int(post_id) == POST:
            return json.loads(json.dumps(self.post))
        return json.loads(json.dumps(self.posts.get(int(post_id), {
            "id": int(post_id),
            "status": "awaiting_human",
            "featured_media": 0,
            "meta": {
                "_hermes_work_state": json.dumps({
                    "version": 2,
                    "state": "human_required",
                    "phase": "media",
                    "blocker": "inline_missing",
                }),
            },
            "content": {"raw": ""},
            "title": {"raw": ""},
        })))

    def list_pending(self, *, page, per_page):
        return []

    def update_post(self, post_id, payload):
        self.write_calls += 1
        raise AssertionError("uncertain isolation must not write WordPress")



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
        json.dumps({
            "version": 1,
            "posts": [{"post_id": 115282, "reason_code": "uncertain_transaction", "reason": "preserve existing quarantine"}],
            "release_history": [],
        }),
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
    other_state = {
        "version": 2,
        "state": "human_required",
        "phase": "media",
        "blocker": "inline_missing",
    }
    previous_lifecycle = {
        "version": 2,
        "state": "ready",
        "phase": "publish",
        "blocker": None,
    }
    historical = {
        115215: {
            "status": "committed",
            "cycle_id": "previous-cycle-115215",
            "cycle_round": 3,
            "authorization_id": "previous-auth-115215",
            "state": previous_lifecycle,
            "readback": True,
            "writer_started": True,
            "resumable": False,
        },
        # This journal has no cycle identity; its run timestamp in the
        # MediaRegistry is the independent proof that it predates this auth.
        115512: {
            "status": "failed_pre_writer",
            "state": previous_lifecycle,
            "readback": False,
            "writer_started": False,
            "resumable": True,
        },
        # The lifecycle in this old journal intentionally differs from the
        # current WordPress lifecycle. Current-auth no-write proof must not
        # mistake that historical state for an execution in this round.
        115338: {
            "status": "committed",
            "cycle_id": CYCLE,
            "cycle_round": 1,
            "authorization_id": "previous-auth-115338",
            "state": previous_lifecycle,
            "readback": True,
            "writer_started": True,
            "resumable": False,
        },
    }
    for other_post_id in (value for value in SELECTED if value != POST):
        if other_post_id == 115524:
            other_run = f"current-run-{other_post_id}"
            other_journal = {
                "status": "committed",
                "post_id": other_post_id,
                "run_id": other_run,
                "cycle_id": CYCLE,
                "cycle_round": 1,
                "authorization_id": AUTH,
                "state": other_state,
                "readback": True,
                "writer_started": True,
                "resumable": False,
                "media_state": "accepted_working",
            }
        else:
            other_run = f"historical-run-{other_post_id}"
            other_journal = {
                **historical[other_post_id],
                "post_id": other_post_id,
                "run_id": other_run,
            }
        (journal_dir / f"{other_post_id}.json").write_text(
            json.dumps(other_journal), encoding="utf-8"
        )
    db = root / "work" / "media_registry.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE media_runs (run_id TEXT, post_id INTEGER, acquisition_outcome TEXT, created_at TEXT);
            CREATE TABLE media_decisions (
                run_id TEXT, post_id INTEGER, disposition TEXT,
                reason_code TEXT, created_at TEXT
            );
            CREATE TABLE media_usage (post_id INTEGER, status TEXT, applied_at TEXT);
            """
        )
        con.execute("INSERT INTO media_runs VALUES (?, ?, ?, ?)", (RUN, POST, "started", "2026-10-23T04:03:17Z"))
        con.execute(
            "INSERT INTO media_decisions VALUES (?, ?, ?, ?, ?)",
            (RUN, POST, "started", "media", "2026-10-23 04:03:17"),
        )
        con.execute(
            "INSERT INTO media_runs VALUES (?, ?, ?, ?)",
            ("current-run-115524", 115524, "readback_confirmed", "2026-10-23T04:03:18Z"),
        )
        con.execute(
            "INSERT INTO media_decisions VALUES (?, ?, ?, ?, ?)",
            ("current-run-115524", 115524, "committed", "readback", "2026-10-23 04:03:18"),
        )
        for other_post_id in (115215, 115512, 115338):
            con.execute(
                "INSERT INTO media_runs VALUES (?, ?, ?, ?)",
                (f"historical-run-{other_post_id}", other_post_id, "historical", "2020-01-01 00:00:00"),
            )


def _args(root):
    client = FakeClient()
    for other_post_id in (value for value in SELECTED if value != POST):
        client.posts[other_post_id] = {
            "id": other_post_id,
            "status": "awaiting_human" if other_post_id == 115524 else "pending",
            "featured_media": 0,
            "meta": {
                "_hermes_work_state": json.dumps({
                    "version": 2,
                    "state": "human_required",
                    "phase": "media",
                    "blocker": "inline_missing",
                }),
            },
            "content": {"raw": ""},
            "title": {"raw": ""},
        }
    return dict(
        root=root,
        client=client,
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


def test_preview_reconciles_audit_shaped_historical_posts_without_inventing_attempts(tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    result = isolate_uncertain_transaction(**args, apply=False)

    by_id = {item["post_id"]: item for item in result["evidence"]["round_post_evidence"]}
    assert by_id[115331]["classification"] == "UNCERTAIN_ISOLATED"
    assert by_id[115524]["classification"] == "COMMITTED_NO_REPLAY"
    assert by_id[115215]["classification"] == "NOT_EXECUTED"
    assert by_id[115512]["classification"] == "NOT_EXECUTED"
    assert by_id[115338]["classification"] == "NOT_EXECUTED"
    assert by_id[115512]["historical_journal_evidence"]["historical_run_id"] == "historical-run-115512"
    previous_lifecycle = {
        "version": 2,
        "state": "ready",
        "phase": "publish",
        "blocker": None,
    }
    assert by_id[115338]["wordpress"]["work_state_sha256"] != transaction_admin._hash_value(
        previous_lifecycle
    )
    assert result["round_classifications"] == {
        "reconciled_post_ids": [115215, 115338, 115512, 115524],
        "not_executed_post_ids": [115215, 115338, 115512],
        "executed_ids": [115524],
        "unresolved_post_ids": [],
    }
    persisted = CycleStateStore(tmp_path).load()
    assert persisted.attempts_total == 0
    assert persisted.attempts_by_post == {}
    assert persisted.rounds == 0
    assert args["client"].write_calls == 0


def test_historical_committed_journal_is_not_current_without_registry_run_identity(tmp_path):
    _fixture(tmp_path)
    db = tmp_path / "work" / "media_registry.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM media_runs WHERE run_id=?", ("current-run-115524",))
    result = isolate_uncertain_transaction(**_args(tmp_path), apply=False)
    by_id = {item["post_id"]: item for item in result["evidence"]["round_post_evidence"]}
    assert by_id[115524]["classification"] == "UNCERTAIN_ISOLATED"
    assert by_id[115215]["classification"] == "NOT_EXECUTED"


def test_identityless_historical_journal_needs_registry_timestamp_proof(tmp_path):
    _fixture(tmp_path)
    db = tmp_path / "work" / "media_registry.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM media_runs WHERE run_id=?", ("historical-run-115512",))
    result = isolate_uncertain_transaction(**_args(tmp_path), apply=False)
    by_id = {item["post_id"]: item for item in result["evidence"]["round_post_evidence"]}
    assert by_id[115512]["classification"] == "UNCERTAIN_ISOLATED"


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
    assert state.rounds == 0
    assert state.attempts_total == 0
    original = json.loads((tmp_path / "work" / "v2-journal" / f"{POST}.json").read_text())
    assert original["status"] == "committing"
    policy = json.loads((tmp_path / "config" / "v2-quarantine.json").read_text())
    assert [row["post_id"] for row in policy["posts"]] == [115282, POST]
    assert policy["posts"][0]["reason_code"] == "uncertain_transaction"
    assert result["wordpress_writes"] == 0
    assert args["client"].write_calls == 0
    assert any(
        line.get("event") == "transaction_uncertain_isolated"
        for line in map(json.loads, (tmp_path / "work/v2-cycle/journal.jsonl").read_text().splitlines())
    )
    event = next(
        line for line in map(json.loads, (tmp_path / "work/v2-cycle/journal.jsonl").read_text().splitlines())
        if line.get("event") == "transaction_uncertain_isolated"
    )
    assert event["reconciled_post_ids"] == [115215, 115338, 115512, 115524]
    assert event["not_executed_post_ids"] == [115215, 115338, 115512]
    assert event["executed_ids"] == [115524]
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


def test_crash_after_checkpoint_before_event_resumes_idempotently(monkeypatch, tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_save = CycleStateStore.save

    def crash_after_checkpoint(self, state, *, event, **fields):
        state.touch()
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.state_path,
            json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        raise RuntimeError("crash between checkpoint and event")

    monkeypatch.setattr(CycleStateStore, "save", crash_after_checkpoint)
    with pytest.raises(RuntimeError, match="between checkpoint and event"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    assert CycleStateStore(tmp_path).load().pending_round is None
    assert not any(
        json.loads(line).get("event") == "transaction_uncertain_isolated"
        for line in (tmp_path / "work/v2-cycle/journal.jsonl").read_text().splitlines()
    )
    monkeypatch.setattr(CycleStateStore, "save", original_save)
    resumed = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    assert resumed["status"] == "applied"
    assert CycleStateStore(tmp_path).load().pending_round is None
    events = [
        json.loads(line)
        for line in (tmp_path / "work/v2-cycle/journal.jsonl").read_text().splitlines()
        if json.loads(line).get("event") == "transaction_uncertain_isolated"
    ]
    assert len(events) == 1
    assert CycleStateStore(tmp_path).load().attempts_total == 0
    assert json.loads((tmp_path / "work/v2-journal" / f"{POST}.json").read_text())["status"] == "committing"


def test_torn_event_append_is_separated_before_recovery_event(monkeypatch, tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_save = CycleStateStore.save

    def crash_during_event_append(self, state, *, event, **fields):
        state.touch()
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.state_path,
            json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write('{"event":"transaction_uncertain_isolated"')
            handle.flush()
        raise RuntimeError("crash during event append")

    monkeypatch.setattr(CycleStateStore, "save", crash_during_event_append)
    with pytest.raises(RuntimeError, match="during event append"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    monkeypatch.setattr(CycleStateStore, "save", original_save)
    resumed = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    assert resumed["status"] == "applied"
    lines = (tmp_path / "work/v2-cycle/journal.jsonl").read_text().splitlines()
    assert lines[-2] == '{"event":"transaction_uncertain_isolated"'
    assert len(transaction_admin._event_records(tmp_path, preview["isolation_id"])) == 1


def test_crash_after_event_before_transition_phase_does_not_duplicate_event(monkeypatch, tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_save = CycleStateStore.save

    def crash_after_event(self, *call_args, **call_kwargs):
        original_save(self, *call_args, **call_kwargs)
        if call_kwargs.get("event") == "transaction_uncertain_isolated":
            raise RuntimeError("crash after event")

    monkeypatch.setattr(CycleStateStore, "save", crash_after_event)
    with pytest.raises(RuntimeError, match="after event"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    monkeypatch.setattr(CycleStateStore, "save", original_save)
    resumed = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    assert resumed["status"] == "applied"
    events = [
        json.loads(line)
        for line in (tmp_path / "work/v2-cycle/journal.jsonl").read_text().splitlines()
        if json.loads(line).get("event") == "transaction_uncertain_isolated"
    ]
    assert len(events) == 1


def test_partial_archive_is_resumed_without_replacing_snapshot(monkeypatch, tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_copy = transaction_admin._copy_verified
    failed = {"once": False}

    def fail_after_first_snapshot(source, destination):
        if destination.name == "journal.jsonl" and not failed["once"]:
            failed["once"] = True
            raise RuntimeError("archive preparation interrupted")
        return original_copy(source, destination)

    monkeypatch.setattr(transaction_admin, "_copy_verified", fail_after_first_snapshot)
    with pytest.raises(RuntimeError, match="archive preparation"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    archive = tmp_path / "work/v2-cycle/archives" / f"uncertain-{preview['isolation_id']}"
    preserved_state = (archive / "state.json").read_bytes()
    assert not (archive / "journal.jsonl").exists()
    monkeypatch.setattr(transaction_admin, "_copy_verified", original_copy)
    resumed = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    assert resumed["status"] == "applied"
    assert (archive / "state.json").read_bytes() == preserved_state
    assert (archive / "journal.jsonl").is_file()


def test_crash_after_archive_record_before_transition_is_recovered(monkeypatch, tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_write = transaction_admin._write_json_once

    def crash_before_transition_record(path, payload):
        if path.name == "isolation-record.json":
            raise RuntimeError("crash before transition record")
        return original_write(path, payload)

    monkeypatch.setattr(transaction_admin, "_write_json_once", crash_before_transition_record)
    with pytest.raises(RuntimeError, match="before transition record"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    archive = tmp_path / "work/v2-cycle/archives" / f"uncertain-{preview['isolation_id']}"
    assert (archive / "quarantine-after.json").is_file()
    assert not (archive / "isolation-record.json").exists()
    monkeypatch.setattr(transaction_admin, "_write_json_once", original_write)
    resumed = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    assert resumed["status"] == "applied"
    assert (archive / "isolation-record.json").is_file()


def test_crash_after_prepared_transition_is_recovered(monkeypatch, tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_apply = transaction_admin._apply_transition

    def crash_after_prepare(root, transition):
        raise RuntimeError("crash after prepared transition")

    monkeypatch.setattr(transaction_admin, "_apply_transition", crash_after_prepare)
    with pytest.raises(RuntimeError, match="after prepared transition"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    transition_path = next((tmp_path / "work/v2-cycle/transaction-isolations").glob("*.json"))
    assert json.loads(transition_path.read_text())["phase"] == "prepared"
    monkeypatch.setattr(transaction_admin, "_apply_transition", original_apply)
    resumed = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    assert resumed["status"] == "applied"


def test_crash_after_quarantine_write_before_phase_marker_is_recovered(monkeypatch, tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_atomic_write = transaction_admin.atomic_write_text
    failed = {"once": False}

    def crash_after_policy_write(path, content):
        original_atomic_write(path, content)
        if Path(path).name == "v2-quarantine.json" and not failed["once"]:
            failed["once"] = True
            raise RuntimeError("crash after quarantine write")

    monkeypatch.setattr(transaction_admin, "atomic_write_text", crash_after_policy_write)
    with pytest.raises(RuntimeError, match="after quarantine write"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    monkeypatch.setattr(transaction_admin, "atomic_write_text", original_atomic_write)
    transition_path = next((tmp_path / "work/v2-cycle/transaction-isolations").glob("*.json"))
    assert json.loads(transition_path.read_text())["phase"] == "prepared"
    resumed = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    assert resumed["status"] == "applied"


def test_crash_after_checkpoint_applied_marker_is_recovered(monkeypatch, tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_atomic_write = transaction_admin.atomic_write_text
    failed = {"once": False}

    def crash_after_checkpoint_marker(path, content):
        original_atomic_write(path, content)
        if Path(path).parent.name == "transaction-isolations" and not failed["once"]:
            payload = json.loads(content)
            if payload.get("phase") == "checkpoint_applied":
                failed["once"] = True
                raise RuntimeError("crash after checkpoint marker")

    monkeypatch.setattr(transaction_admin, "atomic_write_text", crash_after_checkpoint_marker)
    with pytest.raises(RuntimeError, match="after checkpoint marker"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    monkeypatch.setattr(transaction_admin, "atomic_write_text", original_atomic_write)
    resumed = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    assert resumed["status"] == "applied"
    events = [
        json.loads(line)
        for line in (tmp_path / "work/v2-cycle/journal.jsonl").read_text().splitlines()
        if json.loads(line).get("event") == "transaction_uncertain_isolated"
    ]
    assert len(events) == 1


def test_resume_preserves_quarantine_additions_and_rejects_edits(monkeypatch, tmp_path):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_save = CycleStateStore.save

    def crash_before_checkpoint(self, *call_args, **call_kwargs):
        raise RuntimeError("stop before checkpoint")

    monkeypatch.setattr(CycleStateStore, "save", crash_before_checkpoint)
    with pytest.raises(RuntimeError, match="before checkpoint"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    policy_path = tmp_path / "config/v2-quarantine.json"
    policy = json.loads(policy_path.read_text())
    policy["posts"].append({
        "post_id": 115700,
        "reason_code": "human_hold",
        "reason": "added during recovery",
    })
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(CycleStateStore, "save", original_save)
    resumed = isolate_uncertain_transaction(
        **args, apply=True, preview=preview, operator_authorization="joao"
    )
    assert resumed["status"] == "applied"
    preserved = json.loads(policy_path.read_text())
    assert {row["post_id"] for row in preserved["posts"]} == {115282, 115700, POST}

    _fixture(tmp_path / "conflict")
    conflict_args = _args(tmp_path / "conflict")
    conflict_preview = isolate_uncertain_transaction(**conflict_args, apply=False)
    monkeypatch.setattr(CycleStateStore, "save", crash_before_checkpoint)
    with pytest.raises(RuntimeError, match="before checkpoint"):
        isolate_uncertain_transaction(
            **conflict_args,
            apply=True,
            preview=conflict_preview,
            operator_authorization="joao",
        )
    monkeypatch.setattr(CycleStateStore, "save", original_save)
    conflict_policy_path = tmp_path / "conflict/config/v2-quarantine.json"
    conflict_policy = json.loads(conflict_policy_path.read_text())
    conflict_policy["posts"][0]["reason"] = "changed by another operator"
    conflict_policy_path.write_text(json.dumps(conflict_policy), encoding="utf-8")
    with pytest.raises(TransactionIsolationError, match="changed, or truncated"):
        isolate_uncertain_transaction(
            **conflict_args,
            apply=True,
            preview=conflict_preview,
            operator_authorization="joao",
        )
    assert json.loads(conflict_policy_path.read_text())["posts"][0]["reason"] == "changed by another operator"


def test_apply_blocks_if_any_other_selected_post_lacks_proof(tmp_path):
    _fixture(tmp_path)
    (tmp_path / "work/v2-journal/115215.json").unlink()
    args = _args(tmp_path)
    args["client"].posts[115215]["status"] = "awaiting_human"
    preview = isolate_uncertain_transaction(**args, apply=False)
    assert 115215 in preview["round_classifications"]["unresolved_post_ids"]
    assert 115215 not in preview["round_classifications"]["reconciled_post_ids"]
    with pytest.raises(TransactionIsolationError, match="unresolved execution evidence"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert not (tmp_path / "work/v2-cycle/transaction-isolations").exists()


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
