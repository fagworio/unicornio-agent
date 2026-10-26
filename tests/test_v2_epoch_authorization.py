import pytest

from unicornio_editor.pipeline_v2.cycle_state import CycleStateStore


def test_epoch_protected_post_cannot_be_authorized(tmp_path):
    active = tmp_path / "work/v2-epochs/active"
    active.mkdir(parents=True)
    (active / "epoch.json").write_text('{"schema_version":1,"epoch_id":"e","protected_transaction_ids":[115757]}')
    store = CycleStateStore(tmp_path)
    state = store.create({})
    store.start_round(state, round_number=1, request={})
    with pytest.raises(RuntimeError, match="epoch-protected"):
        store.authorize_round(state, [115757])
