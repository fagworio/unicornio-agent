import json
import pytest

from unicornio_editor.pipeline_v2.epoch_protection import EpochProtectionError
from unicornio_editor.workflow import _execute_media_plan


def test_protected_post_cannot_enter_media_upload_path(tmp_path):
    active = tmp_path / "work/v2-epochs/active"
    active.mkdir(parents=True)
    (active / "epoch.json").write_text(json.dumps({"schema_version": 1, "epoch_id": "e", "protected_transaction_ids": [115757]}))
    with pytest.raises(EpochProtectionError):
        _execute_media_plan({"media_plan": [{"url": "https://example.invalid/a.jpg"}]}, object(), object(), tmp_path, post_id=115757)
