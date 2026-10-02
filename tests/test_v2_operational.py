from pathlib import Path

import pytest

from unicornio_editor.pipeline_v2.operational import run_shadow, run_write_one
from unicornio_editor.config import Config


class FakeClient:
    def __init__(self):
        self.posts = {1: {"id": 1, "status": "pending", "meta": {"_hermes_state": "partial", "_hermes_partial_kind": "inline_missing", "_hermes_media_required": "1", "_hermes_media_completed": "0", "_hermes_media_missing": "1"}, "title": {"raw": "x"}}}
        self.updates = []

    def get_post(self, post_id):
        return self.posts[post_id]

    def update_post(self, post_id, payload):
        self.updates.append((post_id, payload))
        self.posts[post_id]["meta"].update(payload.get("meta", {}))
        return self.posts[post_id]


def test_v2_shadow_uses_only_reader_and_reports_zero_writes(tmp_path):
    result = run_shadow(FakeClient(), [1], Path(tmp_path), Path(tmp_path) / "snapshots")
    assert result["mode"] == "shadow"
    assert result["wordpress_writes"] == 0
    assert result["production_writes"] == 0
    assert result["snapshots"] == 1


def test_v2_write_requires_explicit_confirmation(tmp_path):
    config = Config(content_source="x", wordpress_url="https://example.test", wordpress_api_base="/wp-json/wp/v2", dry_run=False)
    with pytest.raises(ValueError, match="--write"):
        run_write_one(FakeClient(), config, Path(tmp_path), 1, {}, allow_write=False)
