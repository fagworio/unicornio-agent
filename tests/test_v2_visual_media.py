from __future__ import annotations

from pathlib import Path

from PIL import Image

from unicornio_editor.config import Config
from unicornio_editor.pipeline_v2.model import FeaturedProgress, FeaturedStatus, InlineMedia, LifecycleState, MediaProgress, Phase, WorkState
from unicornio_editor.pipeline_v2.operational import WordPressStateBackend
from unicornio_editor.pipeline_v2.state_store import StateStore
from unicornio_editor.pipeline_v2.visual_media import audit_visual_media, reconcile_visual_media


class Client:
    def __init__(self):
        self.post = {"id": 114838, "status": "pending", "meta": {}}
        self.writes = 0

    def get_post(self, post_id):
        assert post_id == 114838
        return self.post

    def update_post(self, post_id, payload):
        assert post_id == 114838
        self.writes += 1
        self.post["meta"].update(payload.get("meta") or {})


def _image(path: Path) -> Path:
    Image.new("RGB", (32, 32), "red").save(path, "WEBP")
    return path


def _state(client: Client):
    state = WorkState(
        state=LifecycleState.PENDING, phase=Phase.MEDIA, relevance_approved=True,
        media=MediaProgress(
            required=4,
            featured=FeaturedProgress(FeaturedStatus.VALID, 115130, "https://wp.test/115130.webp"),
            inline=(
                InlineMedia(115131, "https://wp.test/115131.webp", 0),
                InlineMedia(115132, "https://wp.test/115132.webp", 1),
            ),
        ),
    )
    StateStore(WordPressStateBackend(client)).commit(114838, state)
    return state


def test_reconcile_removes_only_sha_proven_duplicates(monkeypatch, tmp_path: Path):
    import unicornio_editor.pipeline_v2.visual_media as module

    client = Client()
    _state(client)
    source = _image(tmp_path / "same.webp")
    monkeypatch.setattr(module, "download_image", lambda *_args, **_kwargs: source)
    config = Config("x", "https://example.test", "https://example.test/wp-json/wp/v2")
    report = audit_visual_media(client, config, tmp_path, 114838)
    assert report["inline_visual_unique"] == 0
    assert {item["media_id"] for item in report["duplicates"]} == {115131, 115132}
    applied = reconcile_visual_media(client, config, tmp_path, 114838, apply=True)
    state = StateStore(WordPressStateBackend(client)).load(114838)
    assert applied["readback"]
    assert state.media.inline == ()
    assert state.media.featured.media_id == 115130
    assert state.media.missing == 4


def test_reconcile_is_dry_run_by_default(monkeypatch, tmp_path: Path):
    import unicornio_editor.pipeline_v2.visual_media as module

    client = Client()
    original = _state(client)
    source = _image(tmp_path / "same.webp")
    monkeypatch.setattr(module, "download_image", lambda *_args, **_kwargs: source)
    report = reconcile_visual_media(client, Config("x", "https://example.test", "https://example.test/wp-json/wp/v2"), tmp_path, 114838)
    assert report["writes"] == 0
    assert StateStore(WordPressStateBackend(client)).load(114838) == original
