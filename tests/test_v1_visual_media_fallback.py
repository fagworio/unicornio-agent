import base64
import random
from email.message import Message
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from unicornio_editor.cli import _apply_editorial_batch, _resolve_media_batch_dispatch
from unicornio_editor.config import Config
from unicornio_editor.media import downloader, search, vision_gate, visual_hash
from unicornio_editor.workflow import apply_editorial
import test_workflow as workflow_fixture


@pytest.fixture
def media_case(monkeypatch):
    config = Config(
        "wordpress", "http://wp.test", "/wp-json/wp/v2",
        dry_run=False, media_source_policy="credit_only",
        vision_enabled=True, vision_api_key="test", vision_detail="low",
    )
    data = BytesIO()
    rng = random.Random(115866)
    picture = Image.frombytes("RGB", (128, 72), rng.randbytes(128 * 72 * 3))
    picture = picture.resize((1280, 720), Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(picture)
    draw.rectangle((90, 80, 930, 400), fill="#e7a466")
    draw.ellipse((295, 100, 710, 490), fill="#311e53")
    draw.polygon(((52, 610), (750, 205), (1160, 700)), fill="#598789")
    picture.save(data, format="JPEG")
    body = data.getvalue()
    calls = []

    def assets(request, **_kwargs):
        response = BytesIO(body)
        response.headers = Message()
        response.headers["Content-Type"] = "image/jpeg"
        return response

    def provider(*, image_url, **kwargs):
        assert image_url.startswith("data:image/")
        with Image.open(BytesIO(base64.b64decode(image_url.split(",", 1)[1]))) as seen:
            assert seen.width > 0 and seen.height > 0
        assert "videogame" in kwargs["prompt"].casefold()
        calls.append(image_url)
        return {"status": "MATCH", "confidence": 0.95, "visual_type": "screenshot"}

    def upload(_client, path, evidence):
        assert calls, "WordPress upload happened without actual Vision on materialized bytes"
        with Image.open(path) as uploaded:
            assert uploaded.format == "WEBP"
        assert evidence["credit_text"]
        return {"id": 90, "source_url": "https://wp.test/videogame-90.webp"}

    # This fixture asserts the Vision/readback gates independently of the
    # media checkpoint's real authenticated GET, which has a separate E2E.
    monkeypatch.setattr("unicornio_editor.workflow._verified_v1_progress_asset", lambda *_args: True)
    monkeypatch.setattr(downloader, "urlopen_remote", assets)
    monkeypatch.setattr(vision_gate, "_call_vision", provider)
    monkeypatch.setattr("unicornio_editor.workflow.upload_image", upload)
    return config, calls


def _cover_webp_response(asset_transport, request, **kwargs):
    """Convert fixture bytes for a WordPress attachment declared as a WebP."""
    raw = asset_transport(request, **kwargs)
    output = BytesIO()
    with Image.open(raw) as original:
        original.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(output, format="WEBP")
    output.seek(0)
    output.headers = Message()
    output.headers["Content-Type"] = "image/webp"
    return output


@pytest.mark.parametrize("source_page", [None, "https://third-party.example/page/42"])
def test_v1_batch_keeps_generic_candidate_pending_vision(tmp_path, monkeypatch, media_case, source_page):
    config, calls = media_case
    candidate = {
        "candidate_id": "generic", "direct_image_url": "https://cdn.example/asset-42.jpg",
        "engine": "yandex", **({"source_page_url": source_page} if source_page else {}),
    }
    hash_requests = []

    def observe_hashes(urls, **_kwargs):
        hash_requests.extend(urls)
        return {}

    monkeypatch.setattr(visual_hash, "image_hashes", observe_hashes)
    monkeypatch.setattr(
        search, "search_web_images_batch",
        lambda queries, **_kwargs: [{"query": queries[0], "candidates": [candidate]}],
    )
    resolved = _resolve_media_batch_dispatch(object(), config, tmp_path, {
        "batch_id": "visual-fallback", "schema_version": 1,
        "posts": [{"post_id": 42, "subject": "videogame", "query": "videogame", "needed": 1, "limit": 5, "role": "inline"}],
    }, full=True)
    row = resolved["posts"][0]
    assert row["candidates"][0]["needs_vision"] is True
    assert row["candidates"][0]["source_page_verified"] is False
    assert row["search"]["coverage"]["accepted_visual_representatives"] == 0
    assert row["search"]["distinct_valid_frames"] == 0
    assert row["candidate_diagnostics"][0]["source_verification_category"] == "origin_unknown"
    assert "vision_batch_file" not in resolved
    assert hash_requests == []
    assert calls == []


@pytest.mark.parametrize("source_page", [None, "https://third-party.example/page/42"])
def test_v1_apply_reaches_real_vision_for_generic_candidate(tmp_path, media_case, source_page):
    config, calls = media_case
    editorial = workflow_fixture.editorial_payload()
    editorial["cleaned_html"] = "<p>Texto sobre videogame e lançamento importante.</p><p>Mais detalhes do evento.</p>"
    editorial["media_plan"] = [{
        "paragraph_index": 0, "direct_image_url": "https://cdn.example/asset-42.jpg",
        "alt_text": "Imagem ilustrativa", "is_featured": False,
        **({"source_page_url": source_page} if source_page else {}),
    }]
    post = workflow_fixture.WorkflowTests().post()
    post["featured_media"] = 0
    client = workflow_fixture.FakeClient(post)
    result = apply_editorial(client, config, tmp_path, 42, editorial)
    assert len(calls) == 1, result["media_plan_results"]
    row = result["media_plan_results"][0]
    assert row["status"] == "accepted"
    assert row["source_page_verified"] is False
    assert row["vision_evidence"]["status"] == "MATCH"


def _generic_editorial(*, featured=False):
    editorial = workflow_fixture.editorial_payload()
    editorial["cleaned_html"] = "<p>Texto sobre videogame e lançamento importante.</p><p>Mais detalhes do evento.</p>"
    editorial["media_plan"] = [{
        "paragraph_index": 0, "direct_image_url": "https://cdn.example/asset-42.jpg",
        "alt_text": "Notícia sobre videogame e lançamento importante",
        "credit_text": "Notícia sobre videogame e lançamento importante",
        "search_query": "videogame",
        "evidence_verdict": "deterministic_match",
        "provenance_mode": "credit_only",
        "is_featured": featured,
    }]
    return editorial


@pytest.mark.parametrize("featured", [False, True])
@pytest.mark.parametrize(("status", "confidence", "kind", "expected"), [
    ("UNRELATED", 0.80, "screenshot", "subject_mismatch"),
    ("MATCH", 0.59, "screenshot", "vision_confidence_insufficient"),
    ("MATCH", 0.60, "gameplay", "accepted"),
    ("MATCH", 0.75, "gameplay", "accepted"),
    ("MATCH", 0.85, "gameplay", "accepted"),
    ("UNRELATED", 0.79, "screenshot", "vision_inconclusive"),
    ("MATCH", 0.95, "infographic", "role_dependent"),
])
def test_genuine_visual_decision_controls_upload(tmp_path, monkeypatch, media_case, featured, status, confidence, kind, expected):
    from unicornio_editor.workflow import _execute_media_plan

    config, calls = media_case
    genuine_provider = vision_gate._call_vision

    def provider(**kwargs):
        result = genuine_provider(**kwargs)
        return {**result, "status": status, "confidence": confidence, "visual_type": kind}

    monkeypatch.setattr(vision_gate, "_call_vision", provider)
    editorial = _generic_editorial(featured=featured)
    editorial["media_plan"][0]["direct_image_url"] = "https://cdn.example/videogame-titulo.jpg?signature=mock"
    rows, _, _ = _execute_media_plan(
        editorial, config, object(), tmp_path, v1_visual_fallback=True,
    )
    assert calls, rows
    assert rows[0]["candidate_url"] == "https://cdn.example/videogame-titulo.jpg"
    assert rows[0]["vision_attempted"] is True
    if expected == "accepted" or (expected == "role_dependent" and not featured):
        assert rows[0]["status"] == "accepted"
        assert rows[0]["source_page_verified"] is False
        assert rows[0]["rights_verified"] is False
        assert rows[0]["credit_text"] == "Crédito: Imagem ilustrativa."
        if kind == "gameplay":
            assert rows[0]["alt_text"].startswith("Cena de gameplay:")
    else:
        assert rows[0]["status"] == "rejected"
        assert rows[0]["reason_code"] == ("role_unsuitable" if expected == "role_dependent" else expected)
    if status == "UNRELATED" and confidence >= 0.80:
        assert len(calls) == 1, "explicit unrelated result must never take contextual fallback"


def _low_edge_cover_transport():
    cover = Image.new("RGB", (1280, 720), "#182535")
    draw = ImageDraw.Draw(cover)
    draw.ellipse((278, 170, 733, 567), fill="#d69d60")
    draw.polygon([(408, 563), (912, 121), (1125, 563)], fill="#5e999d")
    image = BytesIO()
    cover.save(image, format="PNG")
    content = image.getvalue()

    def transport(_request, **_kwargs):
        response = BytesIO(content)
        response.headers = Message()
        response.headers["Content-Type"] = "image/png"
        return response

    return transport


@pytest.mark.parametrize("kind,legacy", [
    ("key_art", False), ("text_banner", False), ("key_art", True),
])
def test_normalized_low_edge_cover_uses_role_vision_only_in_v1_credit_lane(
    tmp_path, monkeypatch, media_case, kind, legacy,
):
    import hashlib
    import json
    from dataclasses import replace

    from unicornio_editor.media.converter import image_is_mostly_flat, prepare_featured_webp
    from unicornio_editor.workflow import _execute_media_plan

    config, calls = media_case
    source = _low_edge_cover_transport()
    original = tmp_path / "original.png"
    original.write_bytes(source(None).getvalue())
    webp = prepare_featured_webp(original, tmp_path / "normalized.webp")
    assert image_is_mostly_flat(webp)  # exercise real old veto, not a mocked threshold
    uploads = []

    def upload(_client, path, evidence):
        assert evidence["credit_text"] == "Crédito: Imagem ilustrativa."
        uploads.append(path.read_bytes())
        return {"id": 90, "source_url": "https://wp.test/videogame-90.webp"}

    def provider(**kwargs):
        calls.append(base64.b64decode(kwargs["image_url"].partition(",")[2]))
        assert calls[-1] == webp.read_bytes()
        return {"status": "MATCH", "confidence": 0.95, "visual_type": kind}

    monkeypatch.setattr(downloader, "urlopen_remote", source)
    monkeypatch.setattr(vision_gate, "_call_vision", provider)
    monkeypatch.setattr("unicornio_editor.workflow.upload_image", upload)
    editorial = _generic_editorial(featured=True)
    editorial["media_plan"][0].update(
        direct_image_url="https://gravatar.com/blog/videogame-cover.png?access=SECRET",
        candidate_id="featured--low-edge", target_id="featured",
    )
    monkeypatch.setenv("UNICORNIO_BATCH_ID", "visual-audit-42")
    if legacy:
        config = replace(config, vision_enabled=False, vision_api_key="")
        # Isolate the legacy conversion guard after its unrelated textual
        # preflight has admitted a cover and Vision is not required.
        monkeypatch.setattr("unicornio_editor.workflow._media_item_rejection", lambda *_a, **_kw: None)
    rows, _, _ = _execute_media_plan(
        editorial, config, object(), tmp_path, post_id=42, v1_visual_fallback=not legacy,
    )
    row = rows[0]
    if legacy:
        assert not calls and not uploads
        assert row["failure_kind"] == "featured_flat_art", row
    elif kind == "text_banner":
        assert len(calls) == 1 and not uploads
        assert row["reason_code"] == "role_unsuitable", row
        assert row["failure_stage"] == "featured_vision", row
    else:
        assert len(calls) == 1 and uploads == calls
        assert row["status"] == "accepted", row
        assert row["vision_evidence"]["visual_type"] == "key_art"
        assert row["rights_verified"] is False

    if not legacy:
        raw = (tmp_path / "work" / "telemetry.jsonl").read_text()
        events = [json.loads(line) for line in raw.splitlines()]
        tagged = [event for event in events if event.get("event") == "media_funnel"
                  and event.get("candidate_id") == "featured--low-edge"]
        stages = [event["stage"] for event in tagged]
        verdict = next(event for event in tagged if event["stage"] == "featured_vision")
        final = next(event for event in tagged if event["stage"] == "candidate_decision")
        assert stages.index("conversion") < stages.index("featured_vision") < stages.index("candidate_decision")
        if kind == "key_art":
            assert stages.index("featured_vision") < stages.index("upload")
        else:
            assert "upload" not in stages
        for event in (verdict, final):
            assert (event["batch_id"], event["post_id"], event["target_id"]) == ("visual-audit-42", 42, "featured")
            assert event["vision_decision_source"] == "vision_provider"
            assert event["vision_image_sha256"] == hashlib.sha256(calls[0]).hexdigest()
            assert event["vision_subject"] == row["vision_evidence"]["subject"]
            assert event["vision_model_identity"] == row["vision_evidence"]["model_identity"]
            assert event["vision_verdict"] == ("accept" if kind == "key_art" else "reject")
        assert final["media_id"] == (90 if kind == "key_art" else None)
        assert "access=SECRET" not in raw and "data:image" not in raw

        if kind == "key_art":
            # The same accepted evidence may be reused; it is not a new
            # provider verdict. No second upload or model call is needed to
            # prove this telemetry property, so stop at the upload boundary.
            def stop_at_upload(*_a, **_kw):
                raise OSError("controlled upload failure")

            monkeypatch.setattr("unicornio_editor.workflow.upload_image", stop_at_upload)
            replay, _, _ = _execute_media_plan(
                editorial, config, object(), tmp_path, post_id=42, v1_visual_fallback=True,
            )
            assert replay[0]["reason_code"] == "media_upload_error"
            assert len(calls) == 1 and len(uploads) == 1
            after = (tmp_path / "work" / "telemetry.jsonl").read_text()[len(raw):]
            cached = next(
                event for line in after.splitlines()
                if (event := json.loads(line)).get("stage") == "featured_vision"
            )
            assert cached["vision_decision_source"] == "cache_same_model_context_bytes"
            assert cached["vision_image_sha256"] == verdict["vision_image_sha256"]


def test_media_resolve_cli_filters_retained_targets_before_candidate_download(
    tmp_path, monkeypatch, media_case, capsys,
):
    import json

    from unicornio_editor import cli

    config, calls = media_case
    retained_id = 73
    original = "https://cdn.example/retained-original.jpg"
    # Discovery can expose both the old external original and an opaque
    # Media Library alias. Neither can fill a different target on retry.
    library_alias = "https://wp.test/opaque-alias.jpg"
    replacement = "https://cdn.example/replacement.jpg"
    queries = []
    downloads = []

    class Client:
        def search_media(self, _subject, *, per_page):
            return [{"id": retained_id, "source_url": library_alias}]

    def candidates(values, *, accept, **_kwargs):
        queries.extend(values)
        rows = []
        for query in values:
            discovered = [
                {"candidate_id": "retained", "direct_image_url": original, "engine": "yandex"},
                {"candidate_id": "replacement", "direct_image_url": replacement, "engine": "yandex"},
            ]
            assert accept(discovered, query) == 0  # V1 provisionals do not prove capacity
            rows.append({"query": query, "candidates": discovered})
        return rows

    def no_download(request, **_kwargs):
        downloads.append(request.full_url)
        pytest.fail("Discovery downloaded candidate pixels prematurely")

    monkeypatch.setattr(cli, "_carregar_env_do_projeto", lambda *_args: None)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "WordPressClient", lambda *_args: Client())
    monkeypatch.setattr(search, "search_web_images_batch", candidates)
    monkeypatch.setattr(downloader, "urlopen_remote", no_download)
    path = tmp_path / "media.input.json"
    path.write_text(json.dumps({
        "schema_version": 1, "batch_id": "targeted-retained", "posts": [{
            "post_id": 42, "subject": "Games", "needed": 2, "featured_required": False,
            "existing_media_ids": [retained_id], "excluded_direct_image_urls": [original],
            "targets": [
                {
                    "target_id": f"root:number-{i}", "role": "inline", "subject": subject,
                    "query": subject, "paragraph_index": i, "item_identity": f"root:number-{i}",
                    "item_title": subject, "section_heading": subject,
                }
                for i, subject in enumerate(("Videogame A", "Videogame B"), start=1)
            ],
        }],
    }))
    assert cli.main(["media-resolve-batch", str(path), "--root", str(tmp_path), "--full"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert queries == ["Videogame A", "Videogame B"]
    for target in result["posts"][0]["targets"]:
        assert target["reuse"] == []
        assert [row["url"] for row in target["candidates"]] == [replacement]
        assert target["candidates"][0]["verdict"] == "vision_pending"
        assert any(
            row["candidate_id"] == "retained" and row["verdict"] == "previously_rejected_candidate"
            for row in target["candidate_diagnostics"]
        )
    assert not calls and not downloads


def test_invalid_image_cannot_reach_vision(tmp_path, monkeypatch, media_case):
    from unicornio_editor.workflow import _execute_media_plan

    config, calls = media_case

    def corrupt(_req, **_kwargs):
        response = BytesIO(b"not an actual raster file")
        response.headers = Message()
        response.headers["Content-Type"] = "image/jpeg"
        return response

    monkeypatch.setattr(downloader, "urlopen_remote", corrupt)
    rows, _, _ = _execute_media_plan(
        _generic_editorial(), config, object(), tmp_path, v1_visual_fallback=True,
    )
    assert not calls
    assert rows[0]["reason_code"] == "invalid_image_format"


def test_v1_provider_failure_is_deferred_not_relevance_rejection(tmp_path, monkeypatch, media_case):
    import json

    from unicornio_editor.workflow import _execute_media_plan

    config, _ = media_case

    def offline(**_kwargs):
        raise vision_gate.VisionGateError("provider offline")

    monkeypatch.setattr(vision_gate, "_call_vision", offline)
    rows, _, _ = _execute_media_plan(
        _generic_editorial(), config, object(), tmp_path, v1_visual_fallback=True,
    )
    assert rows[0]["status"] == "deferred"
    assert rows[0]["vision_attempted"] is True
    assert not rows[0].get("vision_evidence")
    assert rows[0]["reason_code"] == "vision_provider_error"
    assert rows[0]["recoverable"] is True
    events = [
        json.loads(line) for file in tmp_path.rglob("*.jsonl") for line in file.read_text().splitlines()
    ]
    summaries = [row for row in events if row.get("event") == "media_post_summary"]
    assert summaries and summaries[-1]["relevance_reject"] == 0


def test_same_pixels_at_two_urls_cannot_fill_two_slots(tmp_path, media_case):

    from unicornio_editor.workflow import _execute_media_plan

    config, calls = media_case
    editorial = _generic_editorial()
    item = editorial["media_plan"][0]
    editorial["media_plan"].append({
        **item, "paragraph_index": 3, "direct_image_url": "https://other.example/asset-99.jpg",
    })
    rows, _, _ = _execute_media_plan(
        editorial, config, object(), tmp_path, v1_visual_fallback=True,
    )
    assert len(calls) == 1
    assert rows[0]["status"] == "accepted"
    assert rows[1]["reason_code"] == "duplicate_existing_frame"


def test_v1_batch_assembles_then_accepts_pending_candidate_and_reaches_ready(tmp_path, monkeypatch, media_case):
    from unicornio_editor.batch import merge_media_manifest_into_editorial

    config, calls = media_case
    original_assets = downloader.urlopen_remote

    def cover_aware_assets(request, **kwargs):
        if request.full_url.startswith("https://wp.test/uploads/"):
            return _cover_webp_response(original_assets, request, **kwargs)
        return original_assets(request, **kwargs)

    monkeypatch.setattr(downloader, "urlopen_remote", cover_aware_assets)
    monkeypatch.setattr(visual_hash, "image_hashes", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        search, "search_web_images_batch",
        lambda queries, **_kwargs: [{
            "query": queries[0], "candidates": [{
                "candidate_id": "generic-ready", "engine": "yandex",
                "direct_image_url": "https://cdn.example/asset-42.jpg",
            }],
        }],
    )
    batch_id = "visual-fallback-ready"
    subject = "Notícia sobre videogame e lançamento importante"
    resolved = _resolve_media_batch_dispatch(object(), config, tmp_path, {
        "batch_id": batch_id, "schema_version": 1,
        "posts": [{
            "post_id": 42, "subject": subject, "query": subject, "needed": 1,
            "limit": 5, "role": "inline", "featured_required": False,
        }],
    })
    editorial = workflow_fixture.editorial_payload()
    paragraph = (
        "<p>O videogame ganhou novidades importantes e mais detalhes no lançamento. "
        "A atualização inclui ajustes que ampliam a exploração e muda a forma de apresentar "
        "as missões na primeira hora de jogo. A equipe reuniu as mudanças em um guia público.</p>"
    )
    existing = (
        '<figure><img src="https://s3.example/noticia-importante-2.webp" '
        f'alt="{subject}" width="800" height="450" />'
        '<figcaption>Crédito: Imagem ilustrativa.</figcaption></figure>'
    )
    editorial["cleaned_html"] = paragraph * 5 + existing + paragraph * 2
    merged, assembly = merge_media_manifest_into_editorial({
        "batch_id": batch_id, "items": [{"post_id": 42, "editorial": editorial}],
    }, resolved["audit"], media_source_policy="credit_only")
    assert assembly["assembled"] == 1
    prepared = merged["items"][0]["editorial"]
    assert prepared["media_plan"][0]["evidence_verdict"] == "vision_pending"
    post = workflow_fixture.WorkflowTests().post()
    client = workflow_fixture.FakeClient(post)
    result = apply_editorial(client, config, tmp_path, 42, prepared)
    assert result["status"] == "ready", result
    assert client.post["meta"]["_hermes_state"] == "ready"
    assert client.post["meta"]["_hermes_ready_hash"]
    assert "https://wp.test/videogame-90.webp" in client.post["content"]["raw"]
    assert client.post["status"] == "pending"
    assert calls


def test_all_new_credit_only_candidates_require_pixels_and_are_not_preflight_verified(tmp_path, media_case):
    from unicornio_editor.cli import _compact_media_validate
    from unicornio_editor.workflow import validate_media_plan

    config, calls = media_case
    editorial = _generic_editorial()
    editorial["media_plan"][0]["direct_image_url"] = "https://cdn.example/videogame-titulo-importante.jpg"
    preflight = validate_media_plan(object(), editorial, config=config, root=tmp_path, v1_visual_fallback=True)
    compact = _compact_media_validate(editorial, preflight, audit="")
    assert preflight["valid"] == 0
    assert preflight["rejected"] == []
    assert compact["vision_pending"] == [0]
    assert compact["capacity"]["valid"] == 0
    assert compact["capacity"]["vision_pending"] == 1
    assert calls == []


def test_pending_candidates_require_enforced_url_policy(tmp_path, monkeypatch, media_case):
    from unicornio_editor.media.url_safety import URLSafetyError
    from unicornio_editor.workflow import _execute_media_plan

    config, calls = media_case
    seen = []

    def refuse(request, *, mode, **kwargs):
        seen.append((request.full_url, mode))
        raise URLSafetyError("non-public test endpoint")

    monkeypatch.setattr(downloader, "urlopen_remote", refuse)
    rows, _, _ = _execute_media_plan(
        _generic_editorial(), config, object(), tmp_path, v1_visual_fallback=True,
    )
    assert seen and all(url == "https://cdn.example/asset-42.jpg" and mode == "enforce" for url, mode in seen)
    assert not calls
    assert rows[0]["status"] == "error"
    assert rows[0]["failure_stage"] == "download"
    assert rows[0]["reason_code"] == "unsafe_url"
    assert not rows[0]["vision_attempted"]


@pytest.mark.parametrize(("mutate_readback", "inline_deficit", "retry_readback"), [
    (False, False, ""), (True, False, ""), (True, True, ""),
    (False, True, "same"), (False, True, "flat_same"),
    (False, True, "changed"), (False, True, "unavailable"),
    (False, True, "wrong_id"), (False, True, "legacy"), (False, True, "context_changed"),
    (False, True, "small_legacy"), (False, True, "url_drift"), (False, True, "media_rest_timeout"),
    (False, True, "unavailable_full_inline"), (False, True, "media_deleted"),
    (False, True, "mixed_human_unavailable"), (False, True, "dry_deleted"),
])
def test_generic_featured_ready_requires_actual_wordpress_attachment_bytes(
    tmp_path, monkeypatch, media_case, mutate_readback, inline_deficit, retry_readback,
):
    from dataclasses import replace
    from pathlib import Path

    from unicornio_editor.batch import build_media_resolve_batch_input, prepare_batch
    from unicornio_editor.wordpress import WordPressError

    config, calls = media_case
    if not mutate_readback:
        original_call = vision_gate._call_vision
        monkeypatch.setattr(vision_gate, "_call_vision", lambda **kwargs: original_call(**kwargs) | {"confidence": 0.60})
    real_assets = _low_edge_cover_transport() if retry_readback == "flat_same" else downloader.urlopen_remote
    wp_url = "https://wp.test/image-90.webp"
    original_cover_url = "https://wp.test/wrong-original-cover.webp"
    uploaded = []
    readback_requests = []
    original_cover_reads = []
    retrying = False

    class Client(workflow_fixture.FakeClient):
        def get_media(self, media_id):
            assert media_id in (7, 90)
            if media_id == 90 and retrying:
                if retry_readback == "media_rest_timeout":
                    raise TimeoutError("controlled attachment metadata timeout")
                if retry_readback in {"media_deleted", "dry_deleted"}:
                    raise WordPressError("missing", status_code=404, api_code="rest_post_invalid_id")
            return {
                "id": 91 if media_id == 90 and retrying and retry_readback == "wrong_id" else media_id,
                "source_url": (
                    "https://wp.test/changed-cover-90.webp"
                    if media_id == 90 and retrying and retry_readback == "url_drift"
                    else wp_url if media_id == 90 else original_cover_url
                ),
                "title": {"rendered": "Imagem 90"}, "alt_text": "Imagem ilustrativa",
                "mime_type": "image/webp", "media_details": {"width": 1280, "height": 720},
            }

        def move_to_status(self, post_id, status):
            result = super().move_to_status(post_id, status)
            self.post["status"] = status
            return result

    def assets(request, *, mode, **kwargs):
        if request.full_url == original_cover_url:
            original_cover_reads.append(mode)
        if request.full_url == wp_url:
            readback_requests.append(mode)
            if retrying and retry_readback in {"unavailable", "unavailable_full_inline", "mixed_human_unavailable"}:
                raise TimeoutError("controlled retained attachment readback timeout")
            if not mutate_readback:
                result = BytesIO(uploaded[0])
                if retrying and retry_readback in {"changed", "small_legacy"}:
                    result = BytesIO()
                    size = (640, 360) if retry_readback == "small_legacy" else (1280, 720)
                    Image.new("RGB", size, "#6865a5").save(result, format="WEBP")
                    result.seek(0)
                result.headers = Message()
                result.headers["Content-Type"] = "image/webp"
                return result
        return real_assets(request, mode=mode, **kwargs)

    def upload(_client, path, evidence):
        assert calls
        uploaded.append(Path(path).read_bytes())
        assert uploaded[0] == base64.b64decode(calls[0].partition(",")[2])
        return {"id": 90, "source_url": wp_url}

    monkeypatch.setattr(downloader, "urlopen_remote", assets)
    monkeypatch.setattr("unicornio_editor.workflow.upload_image", upload)
    monkeypatch.setattr(visual_hash, "image_hashes", lambda *_args, **_kwargs: {})
    editorial = workflow_fixture.editorial_payload()
    editorial["media_plan"] = _generic_editorial(featured=True)["media_plan"]
    if inline_deficit:
        editorial["cleaned_html"] = "<p>Texto revisado sobre videogame e lançamento.</p>"
    post = workflow_fixture.WorkflowTests().post()
    post["featured_media"] = 7 if retry_readback else 0
    client = Client(post)
    original_content = post["content"]["raw"]

    def apply_in_session(cycle):
        monkeypatch.setenv("HERMES_SESSION_ID", f"featured-readback-{cycle}")
        return _apply_editorial_batch(client, config, tmp_path, {
            "batch_id": f"featured-readback-{cycle}", "schema_version": 1,
            "items": [{"post_id": 42, "editorial": editorial}],
        }, compact=False)["posts"][0]

    result = apply_in_session(1)
    assert readback_requests == ["enforce"]
    assert len(calls) == 1, "identical approved bytes reuse the exact cache, changed bytes cannot inherit it"
    if inline_deficit:
        import json

        assert result["status"] == "partial", result
        partial = json.loads((tmp_path / "backups/42/editorial.partial.json").read_text())
        expected_featured_status = "vision_rejected" if mutate_readback else "valid"
        assert partial["featured"]["status"] == expected_featured_status
        assert partial["completed"] == 0 and partial["missing"] == 2
        assert partial["no_progress_attempts"] == (1 if mutate_readback else 0)
        assert client.post["meta"]["_hermes_no_progress_attempts"] == str(partial["no_progress_attempts"])
        assert not client.post["meta"].get("_hermes_ready_hash")

        if mutate_readback:
            # Emulate the next cron session after the saved cooldown expires.
            client.post["meta"]["_hermes_next_retry_at"] = "2000-01-01T00:00:00+00:00"
            second = apply_in_session(2)
            assert second["status"] == "awaiting_human", second
            assert second["partial"]["processing_passes"] == 2
            assert second["partial"]["no_progress_attempts"] == 2
            assert second["partial"]["featured"]["status"] == "vision_rejected"
            assert client.post["meta"]["_hermes_no_progress_attempts"] == "2"
            assert client.post["meta"]["_hermes_state"] == "awaiting_human"
            assert client.post["status"] == "awaiting_human"
            assert client.post["content"]["raw"] == original_content
            assert not client.post["meta"].get("_hermes_ready_hash")
            assert len(uploaded) == 2
            assert readback_requests == ["enforce", "enforce"]
        else:
            if retry_readback in {"legacy", "small_legacy"}:
                # Existing deployed PARTIAL rows have the attachment ID but
                # do not retain the byte receipt. The current bytes must be read.
                for key in ("sha256", "vision_evidence"):
                    partial["featured"].pop(key, None)
                (tmp_path / "backups/42/editorial.partial.json").write_text(json.dumps(partial))
            retrying = True
            client.post["meta"]["_hermes_next_retry_at"] = "2000-01-01T00:00:00+00:00"
            prepared = prepare_batch(client, config, tmp_path, [42], batch_id="featured-plan-retry")
            assert prepared["prepared"] == 1, prepared
            request = build_media_resolve_batch_input({
                "batch_id": "featured-plan-retry",
                "results": [{
                    "post_id": 42, "status": "ok",
                    "editorial": {**editorial, "title": editorial["seo"]["title"]},
                }],
            }, tmp_path)["posts"][0]
            assert request["featured_required"] is False
            assert request["needed"] == 2
            context_requests = []
            if retry_readback == "context_changed":
                client.post["title"] = {"raw": "Nova análise sobre videogame e atualização da campanha"}

                def unavailable_provider(**kwargs):
                    context_requests.append(kwargs["prompt"])
                    raise vision_gate.VisionGateError("controlled fresh-context failure")

                monkeypatch.setattr(vision_gate, "_call_vision", unavailable_provider)
            if retry_readback == "unavailable_full_inline":
                editorial["cleaned_html"] = workflow_fixture.editorial_payload()["cleaned_html"]
            if retry_readback == "mixed_human_unavailable":
                editorial["cleaned_html"] = "<h4>Novidades no videogame</h4>" + editorial["cleaned_html"]
                client.post["meta"]["_hermes_attempts"] = str(config.max_rework_attempts - 1)
            if retry_readback == "dry_deleted":
                config = replace(config, dry_run=True)
            writes_before_retry = list(client.updated)
            receipt_path = tmp_path / "backups/42/editorial.partial.json"
            before_retry = receipt_path.read_bytes()
            second = apply_in_session(2)
            if retry_readback == "mixed_human_unavailable":
                assert second["state"] == "awaiting_human", second
                assert any(
                    row["name"] == "html_semantics" and row["status"] == "fail"
                    for row in second["checklist"]["items"]
                )
                assert client.post["featured_media"] == 7
                assert not any("featured_media" in row for _, row in client.updated[len(writes_before_retry):])
                assert len(uploaded) == 1
                assert not client.post["meta"].get("_hermes_ready_hash")
                return
            if retry_readback in {"wrong_id", "media_rest_timeout", "media_deleted", "dry_deleted"}:
                assert second["status"] == "error", second
                error = {
                    "wrong_id": "featured_retained_identity_unverified",
                    "media_rest_timeout": "featured_retained_recheck_pending",
                    "media_deleted": "featured_retained_attachment_missing",
                    "dry_deleted": "featured_retained_attachment_missing",
                }[retry_readback]
                assert error in second["error"]
                assert client.updated == writes_before_retry
                if retry_readback == "media_deleted":
                    assert json.loads(receipt_path.read_bytes())["featured"]["status"] == "vision_rejected"
                    # Even if the old original looks valid, it is not the
                    # deleted working attachment; ask for a real replacement.
                    retry_id = "featured-plan-replace"
                    further = prepare_batch(client, config, tmp_path, [42], batch_id=retry_id)
                    assert further["prepared"] == 1, further
                    replacement = build_media_resolve_batch_input({
                        "batch_id": retry_id, "results": [{
                            "post_id": 42, "status": "ok",
                            "editorial": {**editorial, "title": editorial["seo"]["title"]},
                        }],
                    }, tmp_path)["posts"][0]
                    assert replacement["featured_required"] is True
                else:
                    assert receipt_path.read_bytes() == before_retry
                assert len(uploaded) == 1 and len(calls) == 1
                assert not client.post["meta"].get("_hermes_ready_hash")
                return
            assert second["status"] == "partial", second
            featured = second["partial"]["featured"]
            expected = {
                "same": "valid", "flat_same": "valid", "legacy": "valid", "changed": "vision_rejected",
                "small_legacy": "vision_rejected", "url_drift": "vision_rejected",
                "unavailable": "verification_pending", "unavailable_full_inline": "verification_pending",
                "context_changed": "verification_pending",
            }[retry_readback]
            assert featured["status"] == expected, second["checklist"]["items"]
            inline_completed = retry_readback == "unavailable_full_inline"
            assert second["partial"]["completed"] == (2 if inline_completed else 0)
            assert second["partial"]["missing"] == (0 if inline_completed else 2)
            assert len(uploaded) == 1
            assert len(calls) == 1
            assert len(context_requests) == (1 if retry_readback == "context_changed" else 0)
            assert original_cover_reads == []
            attempts = {"url_drift": 1, "unavailable": 4, "unavailable_full_inline": 4}.get(retry_readback, 2)
            assert readback_requests == ["enforce"] * attempts
            if retry_readback == "legacy":
                assert featured["media_url"] == wp_url and featured["sha256"]
                assert featured["vision_evidence"]["image_sha256"] == featured["sha256"]
            if retry_readback == "small_legacy":
                assert any("WebP 1280x720" in row["detail"] for row in second["checklist"]["items"])
            if retry_readback == "url_drift":
                assert any("URL do anexo retido diverge" in row["detail"] for row in second["checklist"]["items"])
            if retry_readback == "changed":
                assert any("bytes do anexo divergem" in row["detail"] for row in second["checklist"]["items"])
            if retry_readback == "unavailable":
                assert second["partial"]["no_progress_attempts"] == 0
                assert featured["media_id"] == 90
                retry_readback = "same"
                client.post["meta"]["_hermes_next_retry_at"] = "2000-01-01T00:00:00+00:00"
                recovered = apply_in_session(3)
                assert recovered["partial"]["featured"]["status"] == "valid", recovered
                assert "reason" not in recovered["partial"]["featured"]
                assert recovered["partial"]["no_progress_attempts"] == 1
                assert len(uploaded) == 1 and len(calls) == 1
            assert client.post["featured_media"] == 7
            assert client.post["content"]["raw"] == original_content
            assert not client.post["meta"].get("_hermes_ready_hash")
    elif mutate_readback:
        assert result["status"] != "ready"
        assert not client.post["meta"].get("_hermes_ready_hash")
    else:
        assert result["status"] == "ready", result
        assert client.post["featured_media"] == 90
        assert client.post["meta"]["_hermes_ready_hash"]


def test_inconclusive_low_uses_same_pixels_and_trusted_editorial_context(tmp_path, monkeypatch, media_case):
    from unicornio_editor.workflow import _execute_media_plan

    config, calls = media_case
    check_pixels = vision_gate._call_vision
    details = []

    def provider(**kwargs):
        default = check_pixels(**kwargs)
        assert "falsa autoria" not in kwargs["prompt"].lower()
        details.append(kwargs["detail"])
        return {**default, "confidence": 0.59 if kwargs["detail"] == "low" else 0.75}

    monkeypatch.setattr(vision_gate, "_call_vision", provider)
    editorial = _generic_editorial()
    editorial["media_plan"][0]["alt_text"] = "Falsa autoria"
    editorial["media_plan"][0]["credit_text"] = "Falsa autoria"
    rows, _, _ = _execute_media_plan(editorial, config, object(), tmp_path, v1_visual_fallback=True)
    assert details == ["low", "high"]
    assert calls[0] == calls[1]
    assert rows[0]["status"] == "accepted"
    assert rows[0]["vision_evidence"]["confidence"] == 0.75


def test_v1_cache_is_post_scoped_and_legacy_decision_threshold_unchanged(tmp_path, monkeypatch, media_case):
    from unicornio_editor.workflow import _execute_media_plan

    config, calls = media_case
    check_pixels = vision_gate._call_vision
    monkeypatch.setattr(vision_gate, "_call_vision", lambda **kwargs: check_pixels(**kwargs) | {"confidence": 0.60})
    for post_id in (42, 43):
        rows, _, _ = _execute_media_plan(
            _generic_editorial(), config, object(), tmp_path, post_id=post_id, v1_visual_fallback=True,
        )
        assert rows[0]["status"] == "accepted"
    assert calls[0] == calls[1]
    assert len(calls) == 2
    standard = vision_gate.verify_image_subject_decision(
        image_url=calls[0], subject="videogame", api_key="test", base_url=config.vision_base_url,
        model=config.vision_model,
    )
    assert standard["verdict"] == "inconclusive"


@pytest.mark.parametrize("reused", [False, True])
def test_existing_or_reused_featured_uses_v1_cutoff_even_with_inline_deficit(tmp_path, monkeypatch, media_case, reused):
    import json
    from pathlib import Path

    config, calls = media_case
    original_call = vision_gate._call_vision
    monkeypatch.setattr(vision_gate, "_call_vision", lambda **kwargs: original_call(**kwargs) | {"confidence": 0.60})
    real_assets = downloader.urlopen_remote
    uploaded = []

    def assets(request, **kwargs):
        if "/videogame-90.webp" in request.full_url:
            response = BytesIO(uploaded[0])
            response.headers = Message()
            response.headers["Content-Type"] = "image/webp"
            return response
        if not reused and request.full_url.startswith("https://wp.test/uploads/"):
            return _cover_webp_response(real_assets, request, **kwargs)
        return real_assets(request, **kwargs)

    class Client(workflow_fixture.FakeClient):
        def get_media(self, media_id):
            result = super().get_media(media_id)
            if media_id == 90:
                result["source_url"] = "https://wp.test/videogame-90.webp"
            return result

    def upload(_client, path, _evidence):
        uploaded.append(Path(path).read_bytes())
        return {"id": 90, "source_url": "https://wp.test/videogame-90.webp"}

    monkeypatch.setattr(downloader, "urlopen_remote", assets)
    monkeypatch.setattr("unicornio_editor.workflow.upload_image", upload)
    monkeypatch.setattr(visual_hash, "image_hashes", lambda *_args, **_kwargs: {})
    editorial = workflow_fixture.editorial_payload()
    editorial["cleaned_html"] = "<p>Texto revisado sobre videogame e lançamento.</p>"
    post = workflow_fixture.WorkflowTests().post()
    if reused:
        editorial["media_plan"] = [{
            **_generic_editorial(featured=True)["media_plan"][0],
            "media_library_id": 7, "direct_image_url": "https://cdn.example/videogame-cover.jpg",
        }]
    client = Client(post)
    report = apply_editorial(client, config, tmp_path, 42, editorial)
    assert report["status"] == "partial", report
    assert calls  # a real decoding/provider decision path, not textual proof
    manifest = json.loads((tmp_path / "backups/42/editorial.partial.json").read_text())
    assert manifest["featured"]["status"] == "valid"
    if not reused:
        assert not uploaded
    if reused:
        assert manifest["featured"]["status"] == "valid"
        assert manifest["featured"]["media_id"] == 90
        assert len(calls) == 1  # same normalized attachment + role reuses exact decision


@pytest.mark.parametrize("featured", [False, True])
@pytest.mark.parametrize("decision", ["MATCH", "UNRELATED", "unavailable", "prohibited", "corrupt", "duplicate"])
def test_v1_apply_batch_reviews_generic_library_pixels_before_relevance(tmp_path, monkeypatch, media_case, featured, decision):
    from pathlib import Path

    config, calls = media_case
    real_assets = downloader.urlopen_remote
    uploaded = []
    attempts = []

    class Client(workflow_fixture.FakeClient):
        def get_media(self, media_id):
            return {
                "id": media_id,
                "source_url": (
                    "https://secure.gravatar.com/avatar/example" if decision == "prohibited"
                    else f"https://wp.test/assets/{media_id}.{'webp' if media_id == 90 else 'jpg'}"
                ),
                "title": {"rendered": "Image"}, "alt_text": "",
                "media_details": {"width": 1280, "height": 720},
            }

    def assets(request, *, mode, **kwargs):
        assert mode == "enforce"
        if request.full_url.endswith("/90.webp"):
            result = BytesIO(uploaded[0])
            result.headers = Message()
            result.headers["Content-Type"] = "image/webp"
            return result
        assert request.full_url.endswith("/7.jpg")
        if decision == "corrupt":
            result = BytesIO(b"invalid image bytes")
            result.headers = Message()
            result.headers["Content-Type"] = "image/jpeg"
            return result
        return real_assets(request, mode=mode, **kwargs)

    real_vision = vision_gate._call_vision

    def provider(**kwargs):
        attempts.append(kwargs["image_url"])
        if decision == "unavailable":
            raise vision_gate.VisionGateError("test temporary outage")
        return real_vision(**kwargs) | {"status": "MATCH" if decision == "duplicate" else decision}

    def upload(_client, path, evidence):
        assert calls and decision in {"MATCH", "duplicate"}
        uploaded.append(Path(path).read_bytes())
        assert uploaded[0] == base64.b64decode(calls[0].partition(",")[2])
        assert evidence["credit_text"] == "Crédito: Imagem ilustrativa."
        return {"id": 90, "source_url": "https://wp.test/assets/90.webp"}

    monkeypatch.setattr(downloader, "urlopen_remote", assets)
    monkeypatch.setattr(vision_gate, "_call_vision", provider)
    monkeypatch.setattr("unicornio_editor.workflow.upload_image", upload)
    monkeypatch.setattr(visual_hash, "image_hashes", lambda *_a, **_kw: {})
    editorial = _generic_editorial(featured=featured)
    editorial["media_plan"][0].update({
        "media_library_id": 7, "direct_image_url": "https://index.example/generic.jpg",
        "alt_text": "Imagem", "credit_text": "",
    })
    if decision == "duplicate":
        editorial["media_plan"].append({**editorial["media_plan"][0], "paragraph_index": 1, "is_featured": False})
    post = workflow_fixture.WorkflowTests().post()
    post["featured_media"] = 0
    client = Client(post)
    row = _apply_editorial_batch(client, config, tmp_path, {
        "batch_id": "generic-reuse", "schema_version": 1, "items": [{"post_id": 42, "editorial": editorial}],
    }, compact=False)["posts"][0]
    assert len(attempts) == int(decision not in {"prohibited", "corrupt"}), row
    assert len(uploaded) == int(decision in {"MATCH", "duplicate"})
    candidate = row["media_plan_results"][0]
    assert candidate["status"] == {
        "MATCH": "accepted", "UNRELATED": "rejected", "unavailable": "deferred",
        "prohibited": "rejected", "corrupt": "error", "duplicate": "accepted",
    }[decision]
    if decision == "duplicate":
        assert row["media_plan_results"][1]["status"] == "rejected"
        assert not row["media_plan_results"][1]["vision_attempted"]
    assert not client.post["meta"].get("_hermes_ready_hash")


def test_v1_resolve_batch_keeps_library_candidate_provisional_without_suppressing_web_search(tmp_path, monkeypatch, media_case):
    config, calls = media_case
    queries = []
    class Client:
        def search_media(self, subject, *, per_page):
            assert subject == "videogame" and per_page > 0
            return [{"id": 7, "title": {"rendered": "Image"}, "alt_text": "",
                     "source_url": "https://wp.test/assets/7.jpg"}]

    def search_web(values, **_kw):
        queries.extend(values)
        return [{"query": value, "candidates": []} for value in values]

    monkeypatch.setattr(search, "search_web_images_batch", search_web)
    outcome = _resolve_media_batch_dispatch(Client(), config, tmp_path, {
        "batch_id": "library-pending", "schema_version": 1,
        "posts": [{"post_id": 42, "subject": "videogame", "query": "videogame",
                   "needed": 1, "limit": 5, "role": "inline"}],
    }, full=True)["posts"][0]
    assert queries == ["videogame"]
    assert outcome["needed_web"] == 1
    assert outcome["reuse"] == []
    assert len(outcome["candidates"]) == 1
    candidate = outcome["candidates"][0]
    assert candidate["media_library_id"] == 7 and candidate["verdict"] == "vision_pending"
    assert candidate["needs_vision"] is True
    assert not calls


@pytest.mark.parametrize("mode", ["approved", "changed_after_vision", "disabled", "provider_error"])
def test_v1_apply_batch_normalizes_existing_generic_cover_when_exact_pixels_were_approved(tmp_path, monkeypatch, media_case, mode):
    from dataclasses import replace
    from pathlib import Path

    config, calls = media_case
    real_assets = downloader.urlopen_remote
    uploaded = []
    source_reads = []
    if mode == "disabled":
        config = replace(config, vision_enabled=False)
    if mode == "provider_error":
        def failed_vision(**_kwargs):
            raise vision_gate.VisionGateError("test temporary outage")

        monkeypatch.setattr(vision_gate, "_call_vision", failed_vision)

    class Client(workflow_fixture.FakeClient):
        def get_media(self, media_id):
            return {
                "id": media_id,
                "source_url": f"https://wp.test/assets/{media_id}.{'webp' if media_id == 90 else 'jpg'}",
                "title": {"rendered": "Image"}, "alt_text": "",
                "media_details": {"width": 1280, "height": 720} if media_id == 90 else {"width": 1024, "height": 576},
            }

        def upload_media(self, path, **kwargs):
            uploaded.append(Path(path).read_bytes())
            assert uploaded[0] == base64.b64decode(calls[0].partition(",")[2])
            return {"id": 90, "source_url": "https://wp.test/assets/90.webp"}

    def assets(request, **kwargs):
        if request.full_url.endswith("/90.webp"):
            result = BytesIO(uploaded[0])
            result.headers = Message()
            result.headers["Content-Type"] = "image/webp"
            return result
        assert request.full_url.endswith("/7.jpg")
        assert kwargs["mode"] == "enforce"
        source_reads.append(request.full_url)
        if mode == "changed_after_vision" and len(source_reads) > 1:
            return _cover_webp_response(real_assets, request, **kwargs)
        return real_assets(request, **kwargs)

    monkeypatch.setattr(downloader, "urlopen_remote", assets)
    monkeypatch.setattr(visual_hash, "image_hashes", lambda *_a, **_kw: {})
    client = Client(workflow_fixture.WorkflowTests().post())
    result = _apply_editorial_batch(client, config, tmp_path, {
        "batch_id": "generic-existing-normalize", "schema_version": 1,
        "items": [{"post_id": 42, "editorial": workflow_fixture.editorial_payload()}],
    }, compact=False)["posts"][0]
    if mode == "approved":
        assert len(calls) == 1 and len(uploaded) == 1, result
        assert result["status"] == "ready", result
        assert client.post["featured_media"] == 90
    else:
        assert result["status"] != "ready", result
        assert not uploaded
        assert client.post["featured_media"] == 7
        assert not client.post["meta"].get("_hermes_ready_hash")
    assert client.post["status"] == "pending"


@pytest.mark.parametrize("library_outcome", ["unrelated", "deleted"])
def test_v1_library_rejection_can_use_web_reserve_without_inheriting_attachment(
    tmp_path, monkeypatch, media_case, library_outcome,
):
    from unicornio_editor.wordpress import WordPressError

    config, calls = media_case
    fetches = []
    downloads = []
    old_download = downloader.urlopen_remote
    old_provider = vision_gate._call_vision

    class Client:
        def get_media(self, media_id):
            fetches.append(media_id)
            if library_outcome == "deleted":
                raise WordPressError("WordPress HTTP 404", status_code=404, api_code="rest_post_invalid_id")
            return {"id": media_id, "source_url": "https://wp.test/library.jpg"}

    def download(request, **kwargs):
        downloads.append(request.full_url)
        assert kwargs["mode"] == "enforce"
        return old_download(request, **kwargs)

    def provider(**kwargs):
        result = old_provider(**kwargs)
        if library_outcome == "unrelated" and len(calls) == 1:
            return {**result, "status": "UNRELATED", "confidence": 0.95}
        return result

    monkeypatch.setattr(downloader, "urlopen_remote", download)
    monkeypatch.setattr(vision_gate, "_call_vision", provider)
    editorial = _generic_editorial()
    primary = editorial["media_plan"][0]
    primary["media_library_id"] = 7
    reserve = {
        "candidate_id": "independent-web", "direct_image_url": "https://cdn.example/reserve.jpg",
        "provenance_mode": "credit_only", "credit_text": "Crédito: Imagem ilustrativa.",
    }
    from unicornio_editor.workflow import _execute_media_plan
    rows, _, _ = _execute_media_plan(
        editorial, config, Client(), tmp_path, v1_visual_fallback=True, media_fallback_candidates=[reserve],
    )
    assert fetches == [7]
    assert downloads == (["https://wp.test/library.jpg"] if library_outcome == "unrelated" else []) + [reserve["direct_image_url"]]
    assert rows[-1]["status"] == "accepted" and rows[-1]["candidate_id"] == "independent-web"


@pytest.mark.parametrize("outcome", ["visual_cap", "rest_429", "visual_unrelated", "corrupt", "unsafe_url", "asset_dns"])
def test_automatic_v1_existing_cover_goes_through_pixels_and_shared_budget(tmp_path, monkeypatch, media_case, outcome):
    from dataclasses import replace
    from unicornio_editor.batch import (
        build_media_resolve_batch_input, load_media_resolve_batch,
        merge_media_manifest_into_editorial, prepare_batch,
    )
    from unicornio_editor.wordpress import WordPressError

    config, calls = media_case
    config = replace(config, vision_max_low=1, max_source_retries=0)
    post = workflow_fixture.WorkflowTests().post()
    real_provider = vision_gate._call_vision
    fixture_transport = downloader.urlopen_remote
    from unicornio_editor.media.url_safety import URLSafetyError, enforce_remote_url

    def assets(request, *, mode, **kwargs):
        assert mode == "enforce"
        if request.full_url == "http://127.0.0.1:1/asset.jpg":
            # Real safety gate raises before trying the prohibited destination.
            enforce_remote_url(request.full_url, mode=mode)
            pytest.fail("private address passed enforced validation")
        if request.full_url.endswith("/7.jpg") and outcome == "asset_dns":
            raise URLSafetyError("DNS não resolveu o host")
        if request.full_url.endswith("/7.jpg") and outcome == "corrupt":
            result = BytesIO(b"invalid asset bytes")
            result.headers = Message()
            result.headers["Content-Type"] = "image/jpeg"
            return result
        return fixture_transport(request, mode=mode, **kwargs)

    monkeypatch.setattr(downloader, "urlopen_remote", assets)

    class Client(workflow_fixture.FakeClient):
        def get_media(self, media_id):
            if outcome == "rest_429" and media_id == 7:
                raise WordPressError("WordPress HTTP 429", status_code=429)
            if media_id == 90:
                return {"id": media_id, "source_url": "https://wp.test/videogame-90.webp",
                        "mime_type": "image/webp", "media_details": {"width": 1280, "height": 720}}
            return {
                "id": media_id,
                "source_url": (
                    "http://127.0.0.1:1/asset.jpg" if media_id == 7 and outcome == "unsafe_url"
                    else f"https://wp.test/assets/{media_id}.jpg"
                ),
                "title": {"rendered": "Image"}, "alt_text": "",
            }

        def upload_media(self, *args, **kwargs):
            pytest.fail("unapproved imported cover was normalized/uploaded")

    def provider(**kwargs):
        result = real_provider(**kwargs)
        if outcome == "visual_cap":
            return {**result, "status": "UNCERTAIN", "confidence": 0.35}
        if outcome == "visual_unrelated":
            return {**result, "status": "UNRELATED", "confidence": 0.97}
        return result

    monkeypatch.setattr(vision_gate, "_call_vision", provider)
    client = Client(post)
    batch_id = "real-prepare-cover"
    prepared = prepare_batch(client, config, tmp_path, [42], batch_id=batch_id)
    assert prepared["prepared"] == 1 and not calls
    editorial = workflow_fixture.editorial_payload()
    editorial["title"] = editorial["seo"]["title"]
    editorial["cleaned_html"] = (
        "<p>O videogame ganhou novidades importantes no lançamento, segundo a equipe responsável pelo desenvolvimento e seu comunicado.</p>" * 8
    )
    request = build_media_resolve_batch_input({
        "batch_id": batch_id,
        "results": [{"post_id": 42, "status": "ok", "editorial": editorial}],
    }, tmp_path)
    assert request["posts"][0]["featured_required"] is False
    assert request["posts"][0]["needed"] == 2
    assert not calls
    monkeypatch.setattr(visual_hash, "image_hashes", lambda *_a, **_kw: {})
    monkeypatch.setattr(search, "search_web_images_batch", lambda queries, **_kw: [{
        "query": q, "candidates": [{"direct_image_url": "https://cdn.example/body.jpg", "engine": "yandex"}],
    } for q in queries])
    resolved = _resolve_media_batch_dispatch(client, config, tmp_path, load_media_resolve_batch(request["path"]))
    batch, assembly = merge_media_manifest_into_editorial({
        "schema_version": 1, "batch_id": batch_id,
        "items": [{"post_id": 42, "status": "ok", "editorial": editorial}],
    }, resolved["audit"], media_source_policy="credit_only")
    assert assembly["assembled"] == 1
    items = batch["items"][0]["editorial"]["media_plan"]
    assert len(items) == 1 and not items[0]["is_featured"], items
    row = _apply_editorial_batch(client, config, tmp_path, batch, compact=False)["posts"][0]
    assert row["status"] == "partial", row
    assert len(calls) == 1
    assert not client.post["meta"].get("_hermes_ready_hash")
    if outcome in {"visual_cap", "visual_unrelated"}:
        assert row["media_plan_results"][0]["reason_code"] == "vision_budget_exhausted"
    else:
        assert row["media_plan_results"][0]["status"] == "accepted", row
    if outcome in {"rest_429", "asset_dns"}:
        assert row["partial"]["featured"]["status"] == "missing"
    else:
        assert row["partial"]["featured"]["status"] == "vision_rejected"
        assert row["partial"]["featured"]["media_id"] == 7
        from unicornio_editor.batch import _context_for_post
        ctx = _context_for_post(post=client.post, prepared=editorial, batch_id="replacement",
                                config=config, client=client, root=tmp_path)
        assert ctx["featured"]["action"] == "replace"
    assert not any(payload.get("featured_media") for _, payload in client.updated)


def test_v1_plan_preserves_generic_indexed_library_and_web_in_bounded_mix(tmp_path, monkeypatch, media_case):
    from unicornio_editor.media.library_index import register

    config, calls = media_case
    generic = {"id": 7, "source_url": "https://wp.test/assets/asset.jpg", "title": {"rendered": "Image"}}
    register(tmp_path, source_url=generic["source_url"], media_id=7, subject="videogame")

    class Client:
        def get_media(self, media_id):
            assert media_id == 7
            return generic

        def search_media(self, subject, *, per_page):
            return [
                {"id": 8, "source_url": "https://wp.test/assets/videogame-sequel.jpg"},
                {"id": 9, "source_url": "https://wp.test/assets/videogame-cover.jpg"},
            ]

    web_urls = ["https://cdn.example/web1.jpg", "https://cdn.example/web2.jpg"]
    monkeypatch.setattr(search, "search_web_images_batch", lambda queries, **_kw: [
        {"query": q, "candidates": [{"direct_image_url": u, "engine": "yandex"} for u in web_urls]}
        for q in queries
    ])
    outcome = _resolve_media_batch_dispatch(Client(), config, tmp_path, {
        "batch_id": "library-web-mix", "schema_version": 1,
        "posts": [{"post_id": 42, "subject": "videogame", "query": "videogame",
                   "needed": 3, "limit": 3, "role": "inline"}],
    }, full=True)["posts"][0]
    assert outcome["reuse"] == [] and outcome["needed_web"] == 3
    assert outcome["candidates"][0]["media_library_id"] == 7
    assert {c["url"] for c in outcome["candidates"][1:]} == set(web_urls)
    assert all(c["verdict"] == "vision_pending" for c in outcome["candidates"])
    assert not calls
