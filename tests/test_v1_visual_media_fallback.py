import base64
import random
from email.message import Message
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from unicornio_editor.cli import _resolve_media_batch_dispatch
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

    monkeypatch.setattr(downloader, "urlopen_remote", assets)
    monkeypatch.setattr(vision_gate, "_call_vision", provider)
    monkeypatch.setattr("unicornio_editor.workflow.upload_image", upload)
    return config, calls


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


@pytest.mark.parametrize(("mutate_readback", "inline_deficit"), [
    (False, False), (True, False), (False, True), (True, True),
])
def test_generic_featured_ready_requires_actual_wordpress_attachment_bytes(
    tmp_path, monkeypatch, media_case, mutate_readback, inline_deficit,
):
    from pathlib import Path

    config, calls = media_case
    if not mutate_readback:
        original_call = vision_gate._call_vision
        monkeypatch.setattr(vision_gate, "_call_vision", lambda **kwargs: original_call(**kwargs) | {"confidence": 0.60})
    real_assets = downloader.urlopen_remote
    wp_url = "https://wp.test/image-90.webp"
    uploaded = []
    readback_requests = []

    class Client(workflow_fixture.FakeClient):
        def get_media(self, media_id):
            assert media_id == 90
            return {
                "id": 90, "source_url": wp_url,
                "title": {"rendered": "Imagem 90"}, "alt_text": "Imagem ilustrativa",
                "mime_type": "image/webp", "media_details": {"width": 1280, "height": 720},
            }

    def assets(request, *, mode, **kwargs):
        if request.full_url == wp_url:
            readback_requests.append(mode)
            if not mutate_readback:
                result = BytesIO(uploaded[0])
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
    post["featured_media"] = 0
    client = Client(post)
    result = apply_editorial(client, config, tmp_path, 42, editorial)
    assert readback_requests == ["enforce"]
    assert len(calls) == 1, "identical approved bytes reuse the exact cache, changed bytes cannot inherit it"
    if inline_deficit:
        import json

        assert result["status"] == "partial", result
        partial = json.loads((tmp_path / "backups/42/editorial.partial.json").read_text())
        assert partial["featured"]["status"] == ("vision_rejected" if mutate_readback else "valid")
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
    assert manifest["featured"]["status"] != "vision_rejected"
    if reused:
        assert manifest["featured"]["status"] == "valid"
        assert manifest["featured"]["media_id"] == 90
        assert len(calls) == 1  # same normalized attachment + role reuses exact decision
