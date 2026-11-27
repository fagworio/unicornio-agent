from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

import test_workflow as fixture
from unicornio_editor import workflow as wf
from unicornio_editor.batch import batch_directory, build_media_resolve_batch_input
from unicornio_editor.config import load_config


class CoverClient(fixture.FakeClient):
    def __init__(self, post, *, extension="webp", width=1280, height=720):
        super().__init__(post)
        self.assets = {
            7: {
                "id": 7,
                "source_url": f"https://wp.test/uploads/imported-cover.{extension}",
                "mime_type": "image/webp" if extension == "webp" else "image/jpeg",
                "media_details": {"width": width, "height": height},
                "alt_text": "Capa genérica sem entidade literal",
                "title": {"rendered": "Capa original"},
            }
        }
        self.uploads = []

    def get_media(self, media_id):
        return self.assets[media_id]

    def upload_media(self, path, *, filename, alt_text, title, caption):
        with Image.open(path) as image:
            assert image.format == "WEBP"
            assert image.size == (1280, 720)
        self.uploads.append(dict(filename=filename, alt=alt_text, title=title, bytes=Path(path).read_bytes()))
        self.assets[8] = {
            "id": 8,
            "source_url": "https://wp.test/uploads/normalized.webp",
            "mime_type": "image/webp",
            "media_details": {"width": 1280, "height": 720},
            "title": {"rendered": title},
        }
        return self.assets[8]


def config(**kwargs):
    return replace(fixture.WorkflowTests().config(False), v1_original_cover_only=True, **kwargs)


def forbid_cover_vision(monkeypatch):
    call = Mock(side_effect=AssertionError("the original cover must not enter Vision preflight"))
    monkeypatch.setattr(wf, "_validate_featured_candidate_vision", call)
    return call


def test_compliant_original_reaches_ready_without_vision_or_replacement(tmp_path, monkeypatch):
    post = fixture.WorkflowTests().post()
    post["featured_media"] = 7
    client = CoverClient(post)
    payload = fixture.editorial_payload()
    payload["media_plan"] = [fixture.WorkflowTests().media_item(paragraph_index=1, is_featured=True)]
    preflight_vision = forbid_cover_vision(monkeypatch)
    result = wf.apply_editorial(client, config(vision_enabled=True, vision_api_key=""), tmp_path, 42, payload)
    assert result["status"] == "ready", result
    assert result["featured_media"] == post["featured_media"] == 7
    checks = {row["name"]: row for row in result["checklist"]["items"]}
    assert checks["destaque_relevancia"]["status"] == "skip"
    assert checks["destaque_formato"]["status"] == "pass"
    assert client.uploads == []
    preflight_vision.assert_not_called()


def test_jpeg_is_converted_to_webp_from_same_cover_and_reuses_checkpoint(tmp_path, monkeypatch):
    post = fixture.WorkflowTests().post()
    post["featured_media"] = 7
    client = CoverClient(post, extension="jpg", width=1600, height=900)
    cfg = config()
    payload = fixture.editorial_payload()
    inline_format_failure = {**payload, "cleaned_html": "<p>Texto revisado sobre videogame e lançamento.</p>"}
    source = tmp_path / "source.jpg"
    Image.new("RGB", (1600, 900), (110, 40, 170)).save(source)
    requests = []

    def download(url, path, **kwargs):
        requests.append(url)
        data = source.read_bytes() if url == client.assets[7]["source_url"] else client.uploads[0]["bytes"]
        Path(path).write_bytes(data)
        return path

    monkeypatch.setattr(wf, "download_image", download)
    preflight_vision = forbid_cover_vision(monkeypatch)
    first = wf.apply_editorial(client, cfg, tmp_path, 42, inline_format_failure)
    assert first["status"] != "ready"
    assert post["featured_media"] == 7
    assert len(client.uploads) == 1
    assert client.uploads[0]["alt"] == client.assets[7]["alt_text"]
    assert requests.count(client.assets[7]["source_url"]) == 1
    saved = wf.verified_v1_media_progress(client, cfg, tmp_path, post)
    row = saved["rows"][0]
    assert row["original_featured_id"] == 7
    assert row["media_id"] == 8
    assert not row.get("vision_evidence")
    assert row["width"] == 1280 and row["height"] == 720
    post["meta"]["_hermes_next_retry_at"] = "2000-01-01T00:00:00Z"
    resumed = wf.apply_editorial(client, cfg, tmp_path, 42, payload)
    assert resumed["status"] == "ready", resumed
    assert post["featured_media"] == 8
    assert len(client.uploads) == 1
    preflight_vision.assert_not_called()


def test_absent_original_does_not_create_cover(tmp_path, monkeypatch):
    post = fixture.WorkflowTests().post()
    post["featured_media"] = 0
    client = CoverClient(post)
    payload = fixture.editorial_payload()
    payload["media_plan"] = [fixture.WorkflowTests().media_item(paragraph_index=1, is_featured=True)]
    preflight_vision = forbid_cover_vision(monkeypatch)
    result = wf.apply_editorial(client, config(), tmp_path, 42, payload)
    assert result["status"] != "ready"
    assert any(row["name"] == "imagem_destaque" and row["status"] == "fail" for row in result["checklist"]["items"])
    assert client.uploads == []
    assert post["featured_media"] == 0
    preflight_vision.assert_not_called()


@pytest.mark.parametrize("action", ("preserve", "manual"))
def test_media_batch_requests_only_inline(tmp_path, action):
    batch_id = "original-cover"
    directory = batch_directory(tmp_path, batch_id)
    directory.mkdir(parents=True)
    payload = fixture.editorial_payload()
    payload["cleaned_html"] = "<p>Texto sobre videogame e lançamento.</p>" * 12
    context = {"post_id": 42, "title": payload["seo"]["title"], "cleaned_html": payload["cleaned_html"],
               "featured": {"action": action}}
    (directory / "post-42.json").write_text(json.dumps(context), encoding="utf-8")
    result = build_media_resolve_batch_input({
        "batch_id": batch_id, "results": [{"post_id": 42, "status": "ok", "editorial": payload}],
    }, tmp_path)
    row = result["posts"][0]
    assert row["featured_required"] is False
    assert row["needed"] == row["inline_deficit"] == 2


def test_loader_enables_site_cover_policy(monkeypatch):
    monkeypatch.delenv("EDITOR_V1_ORIGINAL_COVER_ONLY", raising=False)
    monkeypatch.setenv("WP_BASE_URL", "https://wp.test")
    monkeypatch.setenv("WP_USER", "user")
    monkeypatch.setenv("WP_APP_PASSWORD", "secret")
    assert load_config().v1_original_cover_only is True


def test_original_only_normalizes_portrait_without_cropping(tmp_path):
    from unicornio_editor.media.converter import prepare_featured_webp

    source = tmp_path / "portrait.jpg"
    Image.new("RGB", (100, 200), (210, 20, 20)).save(source)
    normalized = prepare_featured_webp(source, preserve_portrait=True)
    with Image.open(normalized) as image:
        assert image.size == (1280, 720)
        assert image.getpixel((40, 40)) == (255, 255, 255)
        assert image.getpixel((640, 40))[0] > 180


def test_stale_resolver_envelope_cannot_search_for_featured(tmp_path, monkeypatch):
    from unicornio_editor import cli

    resolver = Mock(side_effect=AssertionError("must not search for a replacement"))
    monkeypatch.setattr(cli, "_resolve_media_batch", resolver)
    with pytest.raises(wf.WorkflowError, match="stale_media_plan_requests_featured"):
        cli._resolve_media_batch_dispatch(None, config(), tmp_path, {
            "posts": [{"post_id": 42, "featured_required": True}],
        })
    resolver.assert_not_called()
