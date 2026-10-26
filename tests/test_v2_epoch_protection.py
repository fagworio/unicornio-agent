import json
from datetime import datetime, timezone

import pytest

from unicornio_editor.pipeline_v2.epoch_protection import (
    EpochProtectionError,
    load_epoch_protection,
)
from unicornio_editor.pipeline_v2.scheduler import select
from unicornio_editor.pipeline_v2.model import WorkState, LifecycleState, Phase, RetryInfo


def test_active_epoch_protects_pending_post_and_allows_other(tmp_path):
    active = tmp_path / "work/v2-epochs/active"
    active.mkdir(parents=True)
    (active / "epoch.json").write_text(json.dumps({
        "schema_version": 1,
        "epoch_id": "epoch-new",
        "protected_transaction_ids": [115757],
    }))
    policy = load_epoch_protection(tmp_path)
    assert policy.is_protected(115757)
    assert not policy.is_protected(12)
    state = {pid: WorkState(state=LifecycleState.PENDING, phase=Phase.RELEVANCE, retry=RetryInfo()) for pid in (115757, 12)}
    candidates = [(pid, {"post": {"id": pid, "status": "pending"}}) for pid in (115757, 12)]
    chosen = select(candidates, type("Store", (), {"load": lambda self, pid: state[pid]})(), limit=5, excluded_post_ids=policy.protected_ids)
    assert [row[0] for row in chosen] == [12]


def test_malformed_active_epoch_fails_closed(tmp_path):
    active = tmp_path / "work/v2-epochs/active"
    active.mkdir(parents=True)
    (active / "epoch.json").write_text("not-json")
    with pytest.raises(EpochProtectionError):
        load_epoch_protection(tmp_path)
