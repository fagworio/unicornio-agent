import json

from unicornio_editor.pipeline_v2.model import Phase, WorkState
from unicornio_editor.pipeline_v2.state_store import StateStore


class Backend:
    def __init__(self, value):
        self.value = value
    def get(self, post_id):
        return self.value
    def put(self, post_id, value):
        self.value = value


def test_state_store_reads_json_string_from_registered_wordpress_meta():
    state = WorkState(phase=Phase.MEDIA)
    backend = Backend({"_hermes_work_state": json.dumps(state.to_dict())})
    assert StateStore(backend).load(1) == state
