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
from unicornio_editor.pipeline_v2.cycle_runtime import (
    _historical_journal_evidence,
    _parse_registry_timestamp,
    _registry_timestamp_audit,
)
from unicornio_editor.media.registry import connect
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
        self.media_get_calls = []
        self.write_calls = 0
        self.upload_calls = 0

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

    def get_media(self, media_id):
        self.media_get_calls.append(int(media_id))
        return {
            "id": int(media_id),
            "source_url": f"https://uploads.test/{int(media_id)}.webp",
            "mime_type": "image/webp",
            "media_type": "image",
            "parent": POST,
            "alt_text": "test attachment",
        }

    def upload_media(self, *_args, **_kwargs):
        self.upload_calls += 1
        raise AssertionError("uncertain isolation must never upload media")

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
        "writer_started": True,
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
            CREATE TABLE media_usage (
                post_id INTEGER, status TEXT, applied_at TEXT, run_id TEXT,
                cycle_id TEXT, cycle_round INTEGER, authorization_id TEXT, accepted_at TEXT
            );
            CREATE TABLE media_operations (
                post_id INTEGER, run_id TEXT, cycle_id TEXT, cycle_round INTEGER,
                authorization_id TEXT, started_at TEXT, operation_status TEXT
            );
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


def _audit_115181_fixture(root: Path):
    cycle_id = "2a5c2e324e20479180b8f4e99c348b55"
    authorization_id = "78440ceb2d234d93b8ad9f2d7f392b5c"
    target_run_id = "d5cdbed6eaee42e0a1ab2d473ef2867f"
    selected_ids = [115181, 200001, 200002, 200003, 200004]
    root = Path(root)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "v2-quarantine.json").write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}), encoding="utf-8"
    )
    store = CycleStateStore(root)
    state = CycleState(cycle_id=cycle_id, phase="ADMISSION", status="running", config={"ready_target": 5})
    state.pending_round = {
        "round_number": 1,
        "authorization_id": authorization_id,
        "preview_selected_ids": selected_ids,
        "request": {"batch_size": 5},
    }
    store.save(
        state,
        event="round_authorized",
        round_number=1,
        authorization_id=authorization_id,
        selected_ids=selected_ids,
        at="2026-10-23T23:37:47.140176+00:00",
    )
    journal_dir = root / "work" / "v2-journal"
    journal_dir.mkdir(parents=True)
    historical_journal = {
        "post_id": 115181,
        "run_id": target_run_id,
        "cycle_id": "98c32bba7ec5494aa1ef26e3bf9554b5",
        "cycle_round": 3,
        "authorization_id": "4c8ec7675c994e83920305db0688c836",
        "status": "committed",
        "readback": True,
        "resumable": False,
        "state": {"version": 2, "state": "pending", "phase": "media", "blocker": None},
        "media_state": "accepted_working",
    }
    (journal_dir / "115181.json").write_text(json.dumps(historical_journal), encoding="utf-8")
    current_state = {
        "version": 2,
        "state": "human_required",
        "phase": "media",
        "blocker": "inline_missing",
    }
    posts = {
        post_id: {
            "id": post_id,
            "status": "pending",
            "featured_media": 0,
            "meta": {"_hermes_work_state": json.dumps(current_state)},
            "content": {"raw": ""},
            "title": {"raw": ""},
        }
        for post_id in selected_ids
    }

    class Client:
        def __init__(self):
            self.posts = posts
            self.write_calls = 0

        def get_post(self, post_id):
            return json.loads(json.dumps(self.posts[int(post_id)]))

        def update_post(self, post_id, payload):
            self.write_calls += 1
            raise AssertionError("uncertain isolation must never repeat a WordPress write")

    client = Client()
    run_ids = {115181: target_run_id}
    with connect(root) as db:
        asset_id = db.execute(
            "INSERT INTO media_assets(canonical_url) VALUES (?) RETURNING asset_id",
            ("https://cdn.test/115181-historical.webp",),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO media_runs(run_id, post_id, created_at) VALUES (?, ?, ?)",
            (target_run_id, 115181, "2026-10-15T23:56:35+00:00"),
        )
        db.execute(
            "INSERT INTO media_usage(post_id, asset_id, role, status, applied_at, run_id, cycle_id, cycle_round, authorization_id, accepted_at) VALUES (?, ?, 'inline', 'accepted', NULL, ?, ?, ?, ?, NULL)",
            (115181, asset_id, target_run_id, historical_journal["cycle_id"], 3, historical_journal["authorization_id"]),
        )
        for post_id in selected_ids[1:]:
            run_id = f"current-run-{post_id}"
            run_ids[post_id] = run_id
            journal = {
                "post_id": post_id,
                "run_id": run_id,
                "cycle_id": cycle_id,
                "cycle_round": 1,
                "authorization_id": authorization_id,
                "status": "committed",
                "readback": True,
                "writer_started": True,
                "resumable": False,
                "state": current_state,
                "media_state": "accepted_working",
            }
            (journal_dir / f"{post_id}.json").write_text(json.dumps(journal), encoding="utf-8")
            db.execute(
                "INSERT INTO media_runs(run_id, post_id, created_at) VALUES (?, ?, ?)",
                (run_id, post_id, "2026-10-23T23:37:47.000000+00:00"),
            )
        db.commit()
    return {
        "root": root,
        "client": client,
        "cycle_id": cycle_id,
        "authorization_id": authorization_id,
        "selected_ids": selected_ids,
        "pending_round": 1,
        "post_id": 115181,
        "run_id": target_run_id,
        "run_ids": run_ids,
    }


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




def _multi_isolation_args(root):
    args = _args(root)
    args.update(
        isolated_post_ids=[115331, 115215, 115512],
        run_ids={
            115331: RUN,
            115215: "historical-run-115215",
            115512: "historical-run-115512",
        },
    )
    with sqlite3.connect(root / "work" / "media_registry.sqlite") as con:
        con.executemany(
            "INSERT INTO media_usage(post_id, status, applied_at) VALUES (?, ?, ?)",
            [(115215, "accepted", None), (115512, "accepted", None)],
        )
    return args


def _prewriter_media_fixture(root: Path):
    _fixture(root)
    policy_path = root / "config" / "v2-quarantine.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["posts"].extend([
        {"post_id": 115181, "reason_code": "historical_hold", "reason": "preserve existing quarantine"},
        {"post_id": 115195, "reason_code": "historical_hold", "reason": "preserve existing quarantine"},
    ])
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    args = _args(root)
    journal_path = root / "work" / "v2-journal" / f"{POST}.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal.update({
        "status": "running",
        "protocol_version": 2,
        "writer_started": False,
        "resumable": False,
        "readback": None,
        "media_state": "acquisition_incomplete",
        "media_ids": [115775],
    })
    original_journal = json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    journal_path.write_bytes(original_journal)
    partial_path = root / "backups" / str(POST) / "editorial.partial.json"
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_bytes = json.dumps({
        "inline": {
            "required": 1,
            "accepted": [{"media_id": 115775, "slot": 0}],
        },
        "featured": {"media_id": 0},
    }, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    partial_path.write_bytes(partial_bytes)

    # This post is a second, independently verified commit in the same round.
    committed_id = 115215
    committed_run = "current-run-115215"
    committed_journal = {
        "status": "committed",
        "post_id": committed_id,
        "run_id": committed_run,
        "cycle_id": CYCLE,
        "cycle_round": 1,
        "authorization_id": AUTH,
        "state": {
            "version": 2,
            "state": "human_required",
            "phase": "media",
            "blocker": "inline_missing",
        },
        "readback": True,
        "writer_started": True,
        "resumable": False,
        "media_state": "accepted_working",
    }
    (root / "work" / "v2-journal" / f"{committed_id}.json").write_text(
        json.dumps(committed_journal), encoding="utf-8"
    )
    with sqlite3.connect(root / "work" / "media_registry.sqlite") as db:
        db.execute(
            "INSERT INTO media_runs VALUES (?, ?, ?, ?)",
            (committed_run, committed_id, "readback_confirmed", "2026-10-23T04:03:18Z"),
        )
        db.execute(
            "INSERT INTO media_decisions VALUES (?, ?, ?, ?, ?)",
            (committed_run, committed_id, "committed", "readback", "2026-10-23 04:03:18"),
        )
    args["client"].posts[committed_id] = {
        "id": committed_id,
        "status": "awaiting_human",
        "featured_media": 0,
        "meta": {
            "_hermes_work_state": json.dumps(committed_journal["state"]),
        },
        "content": {"raw": ""},
        "title": {"raw": ""},
    }
    return args, original_journal, partial_bytes


def test_prewriter_media_uncertainty_is_previewed_without_mutating_evidence(tmp_path):
    args, original_journal, partial_bytes = _prewriter_media_fixture(tmp_path)
    result = isolate_uncertain_transaction(**args, apply=False)

    assert result["status"] == "preview"
    assert result["classification"] == "UNCERTAIN_ISOLATED"
    assert result["round_classifications"] == {
        "reconciled_post_ids": [115215, 115338, 115512, 115524],
        "not_executed_post_ids": [115338, 115512],
        "executed_ids": [115215, 115524],
        "unresolved_post_ids": [],
    }
    target = next(
        item for item in result["evidence"]["round_post_evidence"]
        if item["post_id"] == POST
    )
    assert target["classification"] == "UNCERTAIN_ISOLATED"
    assert target["prewriter_media_isolation"]["eligible"] is True
    assert target["prewriter_media_isolation"]["partial_media_ids"] == [115775]
    assert target["media_artifacts"]["partial"]["sha256"] == transaction_admin._hash_bytes(partial_bytes)
    assert target["media_artifacts"]["sidecar"] == {
        "present": False,
        "sha256": "",
        "size_bytes": 0,
        "parse_status": "absent",
        "archive_name": f"editorial-partial-v2-{POST}.json",
    }
    assert target["attachments"]["status"] == "complete"
    assert target["attachments"]["items"][0]["id"] == 115775
    assert target["registry"]["audit_rows"]["run"][0]["acquisition_outcome"] == "started"
    assert target["registry"]["audit_rows"]["usages"] == []
    assert args["client"].media_get_calls == [115775]
    assert args["client"].write_calls == 0
    assert args["client"].upload_calls == 0
    assert (tmp_path / "work" / "v2-journal" / f"{POST}.json").read_bytes() == original_journal
    assert (tmp_path / "backups" / str(POST) / "editorial.partial.json").read_bytes() == partial_bytes
    assert not (tmp_path / "backups" / str(POST) / "editorial.partial.v2.json").exists()
    assert CycleStateStore(tmp_path).load().pending_round is not None




def test_prewriter_media_sidecar_with_matching_identity_is_preview_eligible(tmp_path):
    args, _journal, partial_bytes = _prewriter_media_fixture(tmp_path)
    sidecar = {
        "schema_version": 1,
        "post_id": POST,
        "run_id": RUN,
        "cycle_id": CYCLE,
        "cycle_round": 1,
        "authorization_id": AUTH,
        "checkpoint_sha256": transaction_admin._hash_bytes(partial_bytes),
        "media_ids": [115775],
    }
    (tmp_path / "backups" / str(POST) / "editorial.partial.v2.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )
    result = isolate_uncertain_transaction(**args, apply=False)
    target = next(item for item in result["evidence"]["round_post_evidence"] if item["post_id"] == POST)
    evidence = target["prewriter_media_isolation"]
    assert target["media_artifacts"]["sidecar"]["present"] is True
    assert evidence["sidecar_identity_matches"] is True
    assert evidence["artifacts_valid"] is True
    assert evidence["eligible"] is True
    assert result["wordpress_writes"] == 0




@pytest.mark.parametrize("field", ["checkpoint_sha256", "run_id", "cycle_id", "authorization_id"])
def test_prewriter_media_sidecar_identity_mismatch_is_not_eligible(tmp_path, field):
    args, _journal, partial_bytes = _prewriter_media_fixture(tmp_path)
    sidecar = {
        "schema_version": 1,
        "post_id": POST,
        "run_id": RUN,
        "cycle_id": CYCLE,
        "cycle_round": 1,
        "authorization_id": AUTH,
        "checkpoint_sha256": transaction_admin._hash_bytes(partial_bytes),
        "media_ids": [115775],
    }
    sidecar[field] = "mismatch"
    (tmp_path / "backups" / str(POST) / "editorial.partial.v2.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )
    result = isolate_uncertain_transaction(**args, apply=False)
    target = next(item for item in result["evidence"]["round_post_evidence"] if item["post_id"] == POST)
    evidence = target["prewriter_media_isolation"]
    assert evidence["sidecar_identity_matches"] is False
    assert evidence["artifacts_valid"] is False
    assert evidence["eligible"] is False
    assert "valid_media_artifacts" in evidence["missing_criteria"]


def test_prewriter_media_isolation_archives_exact_partial_and_closes_only_proven_round(tmp_path):
    args, original_journal, partial_bytes = _prewriter_media_fixture(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    with pytest.raises(TransactionIsolationError, match="operator"):
        isolate_uncertain_transaction(**args, apply=True, preview=preview)
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert not (tmp_path / "work" / "v2-cycle" / "transaction-isolations").exists()

    result = isolate_uncertain_transaction(
        **args,
        apply=True,
        preview=preview,
        operator_authorization="synthetic-e2e-prewriter-isolation",
    )
    assert result["status"] == "applied"
    assert result["wordpress_writes"] == 0
    assert args["client"].write_calls == 0
    assert args["client"].media_get_calls == [115775, 115775, 115775, 115775]
    state = CycleStateStore(tmp_path).load()
    assert state.pending_round is None
    assert state.rounds == 1
    assert state.attempts_by_post == {115215: 1, 115524: 1}
    assert state.attempts_total == 2
    assert state.seen_post_ids == [115215, 115524]
    assert state.round_reports[-1]["write_replayed"] is False
    assert state.round_reports[-1]["unresolved_post_ids"] == [POST]

    archive = Path(result["archive_dir"])
    assert (archive / f"journal-{POST}.json").read_bytes() == original_journal
    assert (archive / f"editorial-partial-{POST}.json").read_bytes() == partial_bytes
    assert not (archive / f"editorial-partial-v2-{POST}.json").exists()
    record = json.loads((archive / "isolation-record.json").read_text())
    target = next(
        item for item in record["round_post_evidence"] if item["post_id"] == POST
    )
    assert target["classification"] == "UNCERTAIN_ISOLATED"
    assert target["media_artifacts"]["sidecar"]["present"] is False
    assert record["archive_hashes"]["media_partial"] == transaction_admin._hash_bytes(partial_bytes)
    assert record["archive_hashes"]["media_sidecar"] is None
    quarantine = json.loads((tmp_path / "config" / "v2-quarantine.json").read_text())
    assert POST in {row["post_id"] for row in quarantine["posts"]}
    assert 115181 in {row["post_id"] for row in quarantine["posts"]}
    assert 115195 in {row["post_id"] for row in quarantine["posts"]}
    assert (tmp_path / "work" / "v2-journal" / f"{POST}.json").read_bytes() == original_journal
    assert (tmp_path / "backups" / str(POST) / "editorial.partial.json").read_bytes() == partial_bytes
    assert not (tmp_path / "backups" / str(POST) / "editorial.partial.v2.json").exists()
    repeated = isolate_uncertain_transaction(
        **args,
        apply=True,
        preview=preview,
        operator_authorization="synthetic-e2e-prewriter-isolation",
    )
    assert repeated["status"] == "already_applied"
    assert args["client"].upload_calls == 0


@pytest.mark.parametrize("changed", ["partial", "sidecar", "attachment"])
def test_prewriter_media_evidence_change_invalidates_preview(tmp_path, changed):
    args, _journal, _partial = _prewriter_media_fixture(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    if changed == "partial":
        path = tmp_path / "backups" / str(POST) / "editorial.partial.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif changed == "sidecar":
        path = tmp_path / "backups" / str(POST) / "editorial.partial.v2.json"
        path.write_text("{}", encoding="utf-8")
    else:
        original_get_media = args["client"].get_media

        def changed_attachment(media_id):
            value = original_get_media(media_id)
            value["source_url"] = "https://uploads.test/changed.webp"
            return value

        args["client"].get_media = changed_attachment
    with pytest.raises(TransactionIsolationError, match="changed since preview"):
        isolate_uncertain_transaction(
            **args,
            apply=True,
            preview=preview,
            operator_authorization="synthetic-e2e-prewriter-isolation",
        )
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert args["client"].write_calls == 0


def test_prewriter_media_apply_fails_closed_when_attachment_readback_is_unavailable(tmp_path):
    args, _journal, _partial = _prewriter_media_fixture(tmp_path)
    args["client"].get_media = lambda _media_id: (_ for _ in ()).throw(RuntimeError("offline"))
    preview = isolate_uncertain_transaction(**args, apply=False)
    target = next(
        item for item in preview["evidence"]["round_post_evidence"]
        if item["post_id"] == POST
    )
    assert target["attachments"]["status"] == "unavailable"
    with pytest.raises(TransactionIsolationError, match="attachment evidence is incomplete"):
        isolate_uncertain_transaction(
            **args,
            apply=True,
            preview=preview,
            operator_authorization="synthetic-e2e-prewriter-isolation",
        )
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert args["client"].write_calls == 0


def test_prewriter_media_isolation_rejects_contradictory_registry_identity(tmp_path):
    args, _journal, _partial = _prewriter_media_fixture(tmp_path)
    with sqlite3.connect(tmp_path / "work" / "media_registry.sqlite") as db:
        db.execute("ALTER TABLE media_runs ADD COLUMN cycle_id TEXT")
        db.execute("ALTER TABLE media_runs ADD COLUMN cycle_round INTEGER")
        db.execute("ALTER TABLE media_runs ADD COLUMN authorization_id TEXT")
        db.execute(
            "UPDATE media_runs SET cycle_id=?, cycle_round=?, authorization_id=? WHERE run_id=?",
            ("different-cycle", 99, "different-auth", RUN),
        )
    preview = isolate_uncertain_transaction(**args, apply=False)
    target = next(
        item for item in preview["evidence"]["round_post_evidence"]
        if item["post_id"] == POST
    )
    conflicts = target["prewriter_media_isolation"]["registry_identity_conflicts"]
    assert {item["field"] for item in conflicts} == {
        "cycle_id", "cycle_round", "authorization_id",
    }
    with pytest.raises(TransactionIsolationError, match="pre-writer media evidence is incomplete"):
        isolate_uncertain_transaction(
            **args,
            apply=True,
            preview=preview,
            operator_authorization="synthetic-e2e-prewriter-isolation",
        )
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert args["client"].write_calls == 0
    assert args["client"].upload_calls == 0


def test_prewriter_media_isolation_refuses_busy_global_lock(monkeypatch, tmp_path):
    args, _journal, _partial = _prewriter_media_fixture(tmp_path)
    monkeypatch.setattr(transaction_admin.RunSessionLock, "acquire", lambda _self: False)
    result = isolate_uncertain_transaction(**args, apply=False)
    assert result == {
        "status": "blocked",
        "reason": "global_lock_unavailable",
        "wordpress_writes": 0,
    }
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert args["client"].write_calls == 0


def test_prewriter_media_isolation_blocks_when_another_round_post_is_uncertain(tmp_path):
    args, _journal, _partial = _prewriter_media_fixture(tmp_path)
    (tmp_path / "work" / "v2-journal" / "115338.json").unlink()
    args["client"].posts[115338]["status"] = "awaiting_human"
    preview = isolate_uncertain_transaction(**args, apply=False)
    assert 115338 in preview["round_classifications"]["unresolved_post_ids"]
    with pytest.raises(TransactionIsolationError, match="unresolved execution evidence"):
        isolate_uncertain_transaction(
            **args,
            apply=True,
            preview=preview,
            operator_authorization="synthetic-e2e-prewriter-isolation",
        )
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert not (tmp_path / "work" / "v2-cycle" / "transaction-isolations").exists()
    assert args["client"].write_calls == 0
    assert args["client"].upload_calls == 0


def test_prewriter_media_isolation_recovers_after_quarantine_interruption(
    monkeypatch, tmp_path
):
    args, original_journal, partial_bytes = _prewriter_media_fixture(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_write = transaction_admin._write_quarantine_additively

    def write_then_interrupt(*call_args, **call_kwargs):
        original_write(*call_args, **call_kwargs)
        raise RuntimeError("simulated stop after quarantine update")

    monkeypatch.setattr(transaction_admin, "_write_quarantine_additively", write_then_interrupt)
    with pytest.raises(RuntimeError, match="after quarantine update"):
        isolate_uncertain_transaction(
            **args,
            apply=True,
            preview=preview,
            operator_authorization="synthetic-e2e-prewriter-isolation",
        )
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert (tmp_path / "work" / "v2-journal" / f"{POST}.json").read_bytes() == original_journal
    assert (tmp_path / "backups" / str(POST) / "editorial.partial.json").read_bytes() == partial_bytes
    assert not (tmp_path / "backups" / str(POST) / "editorial.partial.v2.json").exists()
    monkeypatch.setattr(transaction_admin, "_write_quarantine_additively", original_write)
    resumed = isolate_uncertain_transaction(
        **args,
        apply=True,
        preview=preview,
        operator_authorization="synthetic-e2e-prewriter-isolation",
    )
    assert resumed["status"] == "applied"
    assert CycleStateStore(tmp_path).load().pending_round is None
    assert args["client"].write_calls == 0
    assert args["client"].upload_calls == 0


def test_prewriter_media_isolation_recovers_between_checkpoint_and_event(
    monkeypatch, tmp_path
):
    args, _journal, _partial = _prewriter_media_fixture(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_save = CycleStateStore.save

    def crash_after_checkpoint_before_event(self, state, *, event, **fields):
        if event == transaction_admin.ISOLATION_EVENT:
            state.touch()
            self.directory.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                self.state_path,
                json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            raise RuntimeError("simulated stop after checkpoint before event")
        return original_save(self, state, event=event, **fields)

    monkeypatch.setattr(CycleStateStore, "save", crash_after_checkpoint_before_event)
    with pytest.raises(RuntimeError, match="checkpoint before event"):
        isolate_uncertain_transaction(
            **args,
            apply=True,
            preview=preview,
            operator_authorization="synthetic-e2e-prewriter-isolation",
        )
    monkeypatch.setattr(CycleStateStore, "save", original_save)
    resumed = isolate_uncertain_transaction(
        **args,
        apply=True,
        preview=preview,
        operator_authorization="synthetic-e2e-prewriter-isolation",
    )
    assert resumed["status"] == "applied"
    state = CycleStateStore(tmp_path).load()
    assert state.pending_round is None
    assert state.attempts_by_post == {115215: 1, 115524: 1}
    events = [
        json.loads(line)
        for line in (tmp_path / "work" / "v2-cycle" / "journal.jsonl").read_text().splitlines()
    ]
    assert sum(item.get("event") == transaction_admin.ISOLATION_EVENT for item in events) == 1
    assert args["client"].write_calls == 0
    assert args["client"].upload_calls == 0


def test_preview_classifies_uncertain_without_writing(tmp_path):
    _fixture(tmp_path)
    result = isolate_uncertain_transaction(**_args(tmp_path), apply=False)
    assert result["status"] == "preview"
    assert result["classification"] == "UNCERTAIN_ISOLATED"
    assert result["wordpress_writes"] == 0
    assert not (tmp_path / "work" / "v2-cycle" / "transaction-isolations").exists()
    assert CycleStateStore(tmp_path).load().pending_round["preview_selected_ids"] == SELECTED



def test_115181_historical_uncertainty_is_archived_without_replay_and_resumes_idempotently(tmp_path):
    args = _audit_115181_fixture(tmp_path)
    journal_path = tmp_path / "work" / "v2-journal" / "115181.json"
    original_journal = journal_path.read_bytes()
    preview = isolate_uncertain_transaction(**args, apply=False)

    assert preview["status"] == "preview"
    assert preview["classification"] == "UNCERTAIN_ISOLATED"
    assert preview["round_classifications"] == {
        "reconciled_post_ids": [200001, 200002, 200003, 200004],
        "not_executed_post_ids": [],
        "executed_ids": [200001, 200002, 200003, 200004],
        "unresolved_post_ids": [],
    }
    target_evidence = next(
        item for item in preview["evidence"]["round_post_evidence"]
        if item["post_id"] == 115181
    )
    assert target_evidence["journal"]["sha256"] == transaction_admin._hash_file(journal_path)
    assert target_evidence["journal"]["authorization_id"] == json.loads(original_journal)["authorization_id"]
    assert target_evidence["classification"] == "UNCERTAIN_ISOLATED"
    assert target_evidence["no_write_evidence"] is None
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert args["client"].write_calls == 0

    applied = isolate_uncertain_transaction(
        **args, apply=True, preview=preview,
        operator_authorization="test-only-isolation-115181",
    )
    assert applied["status"] == "applied"
    assert applied["wordpress_writes"] == 0
    archive = Path(applied["archive_dir"])
    archived_journal_path = archive / "journal-115181.json"
    archived_journal = json.loads(archived_journal_path.read_text())
    assert archived_journal_path.read_bytes() == original_journal
    assert archived_journal["authorization_id"] != args["authorization_id"]
    assert archived_journal["cycle_id"] != args["cycle_id"]
    assert archived_journal["status"] == "committed"
    assert archived_journal["readback"] is True
    record = json.loads((archive / "isolation-record.json").read_text())
    assert record["classification"] == "UNCERTAIN_ISOLATED"
    assert record["round_post_evidence"][0]["journal"]["sha256"] == transaction_admin._hash_file(
        archived_journal_path
    )
    persisted = CycleStateStore(tmp_path).load()
    assert persisted.pending_round is None
    assert persisted.rounds == 1
    assert persisted.attempts_by_post == {200001: 1, 200002: 1, 200003: 1, 200004: 1}
    assert persisted.seen_post_ids == [200001, 200002, 200003, 200004]
    assert persisted.attempts_total == 4
    assert persisted.round_reports[-1]["unresolved_post_ids"] == [115181]
    assert persisted.round_reports[-1]["write_replayed"] is False
    assert journal_path.read_bytes() == original_journal
    repeated = isolate_uncertain_transaction(
        **args, apply=True, preview=preview,
        operator_authorization="test-only-isolation-115181",
    )
    assert repeated["status"] == "already_applied"
    assert CycleStateStore(tmp_path).load().attempts_total == 4
    assert args["client"].write_calls == 0


def test_115181_historical_isolation_recovers_after_interrupted_quarantine_apply(monkeypatch, tmp_path):
    args = _audit_115181_fixture(tmp_path)
    journal_path = tmp_path / "work" / "v2-journal" / "115181.json"
    original_journal = journal_path.read_bytes()
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_write = transaction_admin._write_quarantine_additively

    def write_then_interrupt(*call_args, **call_kwargs):
        original_write(*call_args, **call_kwargs)
        raise RuntimeError("simulated stop after quarantine update")

    monkeypatch.setattr(transaction_admin, "_write_quarantine_additively", write_then_interrupt)
    with pytest.raises(RuntimeError, match="after quarantine update"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview,
            operator_authorization="test-only-isolation-115181",
        )
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert journal_path.read_bytes() == original_journal
    assert args["client"].write_calls == 0
    monkeypatch.setattr(transaction_admin, "_write_quarantine_additively", original_write)
    resumed = isolate_uncertain_transaction(
        **args, apply=True, preview=preview,
        operator_authorization="test-only-isolation-115181",
    )
    assert resumed["status"] == "applied"
    assert CycleStateStore(tmp_path).load().attempts_total == 4
    assert journal_path.read_bytes() == original_journal
    assert args["client"].write_calls == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"post_id": None}, {"run_id": ""}, {"cycle_id": ""}, {"cycle_round": None},
        {"authorization_id": ""},
        {"authorization_id": "78440ceb2d234d93b8ad9f2d7f392b5c"},
        {"status": "failed_pre_writer"}, {"readback": False},
    ],
)
def test_historical_target_journal_without_verifiable_distinct_identity_is_rejected(tmp_path, changes):
    args = _audit_115181_fixture(tmp_path)
    journal_path = tmp_path / "work" / "v2-journal" / "115181.json"
    journal = json.loads(journal_path.read_text())
    journal.update(changes)
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(TransactionIsolationError, match="journal"):
        isolate_uncertain_transaction(**args, apply=False)


def test_missing_usage_timestamps_keep_historical_posts_uncertain(tmp_path):
    _fixture(tmp_path)
    args = _multi_isolation_args(tmp_path)
    result = isolate_uncertain_transaction(**args, apply=False)
    by_id = {item["post_id"]: item for item in result["evidence"]["round_post_evidence"]}
    assert by_id[115215]["classification"] == "UNCERTAIN_ISOLATED"
    assert by_id[115512]["classification"] == "UNCERTAIN_ISOLATED"
    assert by_id[115215]["reason"] == "historical_journal_but_current_authorization_effects_unresolved"
    assert by_id[115512]["reason"] == "historical_journal_but_current_authorization_effects_unresolved"


def test_registry_timestamp_audit_normalizes_iso_offsets_and_rejects_unknowns():
    parsed = _parse_registry_timestamp("2026-10-23T01:00:00-03:00")
    assert parsed is not None
    assert parsed.isoformat() == "2026-10-23T04:00:00+00:00"
    audit = _registry_timestamp_audit(
        [("media_runs", 115215, "2026-10-23 03:30:00"), ("media_usage", 115215, None)],
        authorized_at="2026-10-23T03:00:00+00:00",
    )
    assert audit["current_round_rows"]
    assert audit["missing_or_invalid_timestamps"]
    assert audit["proof_of_absence"] is False



@pytest.mark.parametrize(
    ("run_created_at", "authorized_at", "is_historical"),
    [
        # 10:30 at -03:00 is 13:30 UTC, after the 12:00 UTC authorization.
        ("2026-10-23T10:30:00-03:00", "2026-10-23T12:00:00Z", False),
        # 08:30 at -03:00 is 11:30 UTC, before the 12:00 UTC authorization.
        ("2026-10-23T08:30:00-03:00", "2026-10-23T12:00:00Z", True),
        ("invalid-timestamp", "2026-10-23T12:00:00Z", False),
    ],
)
def test_historical_journal_evidence_compares_normalized_utc_instants(
    tmp_path, run_created_at, authorized_at, is_historical
):
    registry_path = tmp_path / "work" / "media_registry.sqlite"
    registry_path.parent.mkdir(parents=True)
    with sqlite3.connect(registry_path) as db:
        db.executescript(
            """
            CREATE TABLE media_runs (run_id TEXT, post_id INTEGER, created_at TEXT);
            CREATE TABLE media_decisions (run_id TEXT, post_id INTEGER, created_at TEXT);
            CREATE TABLE media_usage (post_id INTEGER, applied_at TEXT);
            """
        )
        db.execute(
            "INSERT INTO media_runs(run_id, post_id, created_at) VALUES (?, ?, ?)",
            ("historical-run", 115215, run_created_at),
        )

    evidence = _historical_journal_evidence(
        tmp_path,
        115215,
        {"run_id": "historical-run"},
        authorized_at=authorized_at,
    )
    assert (evidence is not None) is is_historical
    if is_historical:
        assert evidence["historical_run_created_at_utc"] == "2026-10-23T11:30:00+00:00"
        assert evidence["current_authorized_at_utc"] == "2026-10-23T12:00:00+00:00"


def test_multi_post_isolation_closes_round_with_exact_accounting(tmp_path):
    _fixture(tmp_path)
    args = _multi_isolation_args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    assert preview["round_classifications"] == {
        "reconciled_post_ids": [115338, 115524],
        "not_executed_post_ids": [115338],
        "executed_ids": [115524],
        "unresolved_post_ids": [],
    }
    result = isolate_uncertain_transaction(
        **args,
        apply=True,
        preview=preview,
        operator_authorization="operator-round-close-v1",
    )
    assert result["status"] == "applied"
    state = CycleStateStore(tmp_path).load()
    assert state.pending_round is None
    assert state.rounds == 1
    assert state.attempts_by_post == {115524: 1}
    assert state.seen_post_ids == [115524]
    assert state.attempts_total == 1
    assert state.round_reports[-1]["event"] == "transaction_uncertain_isolated"
    assert state.round_reports[-1]["next_round_number"] == 2
    quarantine = json.loads((tmp_path / "config/v2-quarantine.json").read_text())
    assert {row["post_id"] for row in quarantine["posts"]} == {115282, 115331, 115215, 115512}
    archive = Path(result["archive_dir"])
    for post_id in (115331, 115215, 115512):
        assert (archive / f"journal-{post_id}.json").is_file()
        assert json.loads((tmp_path / "work/v2-journal" / f"{post_id}.json").read_text())["status"] in {"committing", "committed", "failed_pre_writer"}
    assert not any(row.get("post_id") == 115524 for row in quarantine["posts"])


def test_multi_post_isolation_is_idempotent_after_apply(tmp_path):
    _fixture(tmp_path)
    args = _multi_isolation_args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    first = isolate_uncertain_transaction(**args, apply=True, preview=preview, operator_authorization="operator-round-close-v1")
    second = isolate_uncertain_transaction(**args, apply=True, preview=preview, operator_authorization="operator-round-close-v1")
    assert first["status"] == "applied"
    assert second["status"] == "already_applied"
    state = CycleStateStore(tmp_path).load()
    assert state.rounds == 1
    assert state.attempts_by_post == {115524: 1}
    events = [json.loads(line) for line in (tmp_path / "work/v2-cycle/journal.jsonl").read_text().splitlines()]
    assert sum(item.get("event") == "transaction_uncertain_isolated" for item in events) == 1


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
    assert state.rounds == 1
    assert state.attempts_total == 1
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
    assert CycleStateStore(tmp_path).load().attempts_total == 1
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

    def crash_after_prepare(root, transition, client):
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


def test_resume_rejects_changed_additional_isolated_journal(monkeypatch, tmp_path):
    _fixture(tmp_path)
    args = _multi_isolation_args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_apply = transaction_admin._apply_transition

    def crash_after_prepare(root, transition, client):
        raise RuntimeError("crash after prepared transition")

    monkeypatch.setattr(transaction_admin, "_apply_transition", crash_after_prepare)
    with pytest.raises(RuntimeError, match="after prepared transition"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    monkeypatch.setattr(transaction_admin, "_apply_transition", original_apply)

    journal_path = tmp_path / "work" / "v2-journal" / "115215.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["mutated_during_recovery"] = True
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(TransactionIsolationError, match="isolated journal changed"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert args["client"].write_calls == 0


@pytest.mark.parametrize("changed_evidence", ["wordpress", "media_registry"])
def test_resume_revalidates_live_wordpress_and_registry_evidence(
    monkeypatch, tmp_path, changed_evidence
):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_apply = transaction_admin._apply_transition

    def crash_after_prepare(root, transition, client):
        raise RuntimeError("crash after prepared transition")

    monkeypatch.setattr(transaction_admin, "_apply_transition", crash_after_prepare)
    with pytest.raises(RuntimeError, match="after prepared transition"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    monkeypatch.setattr(transaction_admin, "_apply_transition", original_apply)

    if changed_evidence == "wordpress":
        args["client"].post["modified"] = "2026-10-23T15:00:00Z"
    else:
        with sqlite3.connect(tmp_path / "work" / "media_registry.sqlite") as db:
            db.execute(
                "INSERT INTO media_usage(post_id, status, applied_at) VALUES (?, ?, ?)",
                (115524, "accepted", "2026-10-23T15:00:00Z"),
            )

    with pytest.raises(TransactionIsolationError, match="live WordPress, MediaRegistry"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    assert CycleStateStore(tmp_path).load().pending_round is not None
    assert args["client"].write_calls == 0


@pytest.mark.parametrize("tamper", ["duplicate_attempt", "round_report_identity"])
def test_resume_rejects_non_exact_attempts_or_round_report_identity(
    monkeypatch, tmp_path, tamper
):
    _fixture(tmp_path)
    args = _args(tmp_path)
    preview = isolate_uncertain_transaction(**args, apply=False)
    original_save = CycleStateStore.save

    def crash_after_checkpoint_before_event(self, state, *, event, **fields):
        if event == transaction_admin.ISOLATION_EVENT:
            state.touch()
            self.directory.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                self.state_path,
                json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            raise RuntimeError("crash after checkpoint before event")
        return original_save(self, state, event=event, **fields)

    monkeypatch.setattr(CycleStateStore, "save", crash_after_checkpoint_before_event)
    with pytest.raises(RuntimeError, match="checkpoint before event"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    monkeypatch.setattr(CycleStateStore, "save", original_save)

    state_path = tmp_path / "work" / "v2-cycle" / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if tamper == "duplicate_attempt":
        payload["attempts_by_post"]["115524"] = 2
    else:
        payload["round_reports"][-1]["isolation_id"] = "different-isolation"
    atomic_write_text(
        state_path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )

    with pytest.raises(TransactionIsolationError, match="checkpoint no longer matches"):
        isolate_uncertain_transaction(
            **args, apply=True, preview=preview, operator_authorization="joao"
        )
    assert args["client"].write_calls == 0


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
