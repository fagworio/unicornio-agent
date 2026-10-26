import json

import pytest

from unicornio_editor.pipeline_v2.cycle_state import CycleStateStore
from unicornio_editor.pipeline_v2.epoch_protection import EpochProtectionError
from unicornio_editor.workflow import _PostLockGuardedClient


class Handle:
    def assert_owned(self):
        return None


class Client:
    def update_post(self, post_id, payload):
        return {"id": post_id}


def _write_epoch(root, epoch, ids):
    path = root / "work/v2-epochs/active"
    path.mkdir(parents=True, exist_ok=True)
    (path / "epoch.json").write_text(json.dumps({"schema_version": 1, "epoch_id": epoch, "protected_transaction_ids": ids}))


def test_round_authorization_binds_epoch_policy_fingerprint(tmp_path):
    _write_epoch(tmp_path, "e1", [115757])
    store = CycleStateStore(tmp_path)
    state = store.create({})
    store.start_round(state, round_number=1, request={})
    assert state.pending_round["epoch_id"] == "e1"
    assert state.pending_round["epoch_policy_fingerprint"]
    _write_epoch(tmp_path, "e2", [115757])
    with pytest.raises(RuntimeError, match="epoch policy changed"):
        store.authorize_round(state, [12])


def test_guarded_mutator_checks_epoch_before_wordpress_call(tmp_path):
    _write_epoch(tmp_path, "e1", [115757])
    client = _PostLockGuardedClient(Client(), Handle(), root=tmp_path, post_id=115757)
    with pytest.raises(EpochProtectionError):
        client.update_post(115757, {"meta": {}})
