"""V1 keeps multi-game planning failures post-local and binds actual Vision subjects."""

import base64
import io
import json
import random
from email.message import Message
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from unicornio_editor import cli
from unicornio_editor.batch import (
    build_media_resolve_batch_input, load_media_resolve_batch, prepare_batch,
)
from unicornio_editor.config import Config
from unicornio_editor.media import downloader, search, vision_gate, visual_hash
from test_workflow import FakeClient, editorial_payload

ALPHA = "Solar Quest"
BETA = "Lunar Voyage"
PARAGRAPH = "<p>O videogame recebeu uma atualização importante do estúdio com mudanças confirmadas para a próxima temporada.</p>"
ORDINARY = (
    PARAGRAPH * 2
    + f"<h2>{ALPHA}: novidades</h2>" + PARAGRAPH * 3
    + f"<h2>{BETA}: mudanças</h2>" + PARAGRAPH * 3
)
AMBIGUOUS = (
    PARAGRAPH * 2
    + f"<h2>{ALPHA} e {BETA} em comparação</h2>" + PARAGRAPH * 6
)


def payload(html, game_name):
    editorial = editorial_payload()
    editorial.update(cleaned_html=html, game_name=game_name)
    editorial["media_plan"] = []
    return editorial


def post(post_id, html, *, ready=False):
    title = f"{ALPHA} e {BETA}: novidades dos jogos" if post_id == 1 else f"{ALPHA}: novidades do jogo"
    return {
        "id": post_id, "title": {"raw": title}, "status": "pending",
        "featured_media": 0, "content": {"raw": html},
        "meta": {"original_link": "https://source.example/game", **(
            {"_hermes_state": "ready", "_hermes_ready_hash": "immutable-existing-proof"} if ready else {}
        )},
    }


class Client:
    def __init__(self, posts):
        self.posts = {row["id"]: FakeClient(row) for row in posts}

    def get_post(self, post_id):
        return self.posts[post_id].get_post(post_id)

    def update_post(self, post_id, payload):
        return self.posts[post_id].update_post(post_id, payload)

    def search_media(self, *_args, **_kwargs):
        return []


def invoke(monkeypatch, capsys, config, client, root, *args):
    monkeypatch.setattr(cli, "_carregar_env_do_projeto", lambda *a: None)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "WordPressClient", lambda *_args: client)
    # Workflow uses its own imported real WordPressClient for isinstance.
    code = cli.main([*args, "--root", str(root)])
    captured = capsys.readouterr()
    return code, json.loads(captured.out if code == 0 else captured.err)


def result(post_id, html, subject):
    return {"post_id": post_id, "status": "ok", "editorial": payload(html, subject)}


@pytest.mark.parametrize("ids", [(1, 2), (2, 1), (1,)])
def test_ambiguous_post_is_local_at_plan_and_direct_apply_without_media_manifest(tmp_path, monkeypatch, capsys, ids):
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", media_source_policy="credit_only", dry_run=False)
    client = Client([post(1, AMBIGUOUS), post(2, PARAGRAPH * 8)])
    answers = {
        1: result(1, AMBIGUOUS, f"{ALPHA}; {BETA}"),
        2: result(2, PARAGRAPH * 8, ALPHA),
    }
    prepared = prepare_batch(client, config, tmp_path, ids)
    output = Path(prepared["editorial_input"]).with_name("editorial.output.json")
    data = {"schema_version": 1, "batch_id": prepared["batch_id"], "results": [answers[pid] for pid in ids]}
    output.write_text(json.dumps(data))
    request = build_media_resolve_batch_input(data, tmp_path, media_source_policy="credit_only")
    assert request["status"] == ("required" if 2 in ids else "blocked")
    assert request["blocked_posts"][0]["post_id"] == 1
    assert request["blocked_posts"][0]["error_code"] == "multi_game_media_targets_unbound"
    assert [row["post_id"] for row in request["posts"]] == ([2] if 2 in ids else [])
    assert not output.with_name("media.manifest.json").exists()

    # Only isolate the bad item at the real CLI; the valid sibling enters its
    # normal workflow. Stub trailer discovery, never contact WordPress/provider.
    monkeypatch.setattr("unicornio_editor.workflow.find_cached_game_trailer_with_status",
                        lambda *_a, **_kw: (None, "search_failed"))
    code, report = invoke(monkeypatch, capsys, config, client, tmp_path, "apply-batch", str(output))
    assert code == 0, report
    assert [row["post_id"] for row in report["posts"]] == list(ids)
    blocked = next(row for row in report["posts"] if row["post_id"] == 1)
    assert blocked["status"] == "preflight_blocked"
    assert blocked["error_code"] == request["blocked_posts"][0]["error_code"]
    assert not client.posts[1].updated
    if 2 in ids:
        assert next(row for row in report["posts"] if row["post_id"] == 2)["status"] != "preflight_blocked"


def test_ambiguous_legacy_payload_does_not_demote_wordpress_ready(tmp_path, monkeypatch, capsys):
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", media_source_policy="credit_only", dry_run=False)
    client = Client([post(1, AMBIGUOUS, ready=True)])
    output = tmp_path / "editorial.output.json"
    output.write_text(json.dumps({
        "schema_version": 1, "batch_id": "ready-previous", "results": [
            result(1, AMBIGUOUS, f"{ALPHA}; {BETA}"),
        ],
    }))
    code, report = invoke(monkeypatch, capsys, config, client, tmp_path, "apply-batch", str(output))
    assert code == 0
    assert report["posts"][0]["status"] == "noop"
    assert not client.posts[1].updated
    assert client.get_post(1)["meta"]["_hermes_ready_hash"] == "immutable-existing-proof"


@pytest.mark.parametrize("mode", ["reject", "tamper", "accept_inline"])
def test_ordinary_game_h2_targets_reach_real_vision_input_or_fail_closed_on_tamper(
    tmp_path, monkeypatch, capsys, mode,
):
    config = Config(
        "wordpress", "http://wp.test", "/wp-json/wp/v2", media_source_policy="credit_only",
        vision_enabled=True, vision_api_key="synthetic", vision_detail="low", dry_run=False,
    )
    client = Client([post(1, ORDINARY)])
    prepared = prepare_batch(client, config, tmp_path, [1])
    output = Path(prepared["editorial_input"]).with_name("editorial.output.json")
    data = {"schema_version": 1, "batch_id": prepared["batch_id"], "results": [
        result(1, ORDINARY, f"{ALPHA}; {BETA}"),
    ]}
    output.write_text(json.dumps(data))
    request = build_media_resolve_batch_input(data, tmp_path, media_source_policy="credit_only")
    assert request["status"] == "required", request
    targets = load_media_resolve_batch(request["path"])["posts"][0]["targets"]
    inline = [row for row in targets if row["role"] == "inline"]
    assert [row["subject"] for row in inline] == [ALPHA, BETA]
    assert inline[1]["paragraph_index"] - inline[0]["paragraph_index"] >= 3
    assert all(row.get("item_number") is None for row in inline)

    monkeypatch.setattr(search, "search_web_images_batch", lambda queries, **_kw: [
        {"query": query, "candidates": [{"direct_image_url": f"https://cdn.example/{key}.jpg", "engine": "yandex"}]}
        for query in queries
        for key in ["cover" if query == f"{ALPHA} {BETA}" else ("alpha" if query.startswith(ALPHA) else "beta")]
    ])
    resolved = cli._resolve_media_batch_dispatch(client, config, tmp_path, load_media_resolve_batch(request["path"]), full=True)
    for row in resolved["posts"][0]["targets"]:
        assert row["candidates"], row
        assert all(candidate["verdict"] == "vision_pending" for candidate in row["candidates"])
    manifest = Path(resolved["audit"])
    if mode == "tamper":
        raw = json.loads(manifest.read_bytes())
        next(row for row in raw["posts"][0]["targets"] if row["subject"] == BETA)["subject"] = ALPHA
        manifest.write_text(json.dumps(raw))

    sources = {}
    for index, key in enumerate(("alpha", "beta", "cover"), 1):
        source = io.BytesIO()
        raster = Image.frombytes("RGB", (128, 72), random.Random(800 + index).randbytes(128 * 72 * 3))
        raster = raster.resize((1280, 720), Image.Resampling.NEAREST)
        draw = ImageDraw.Draw(raster)
        if key == "alpha":
            draw.rectangle((0, 40, 460, 680), fill="#faf7ba")
            draw.ellipse((610, 60, 1120, 620), fill="#1e163b")
        elif key == "beta":
            draw.rectangle((0, 0, 1280, 245), fill="#223156")
            draw.polygon(((180, 705), (640, 275), (1140, 715)), fill="#f9ba33")
        else:
            draw.ellipse((250, 90, 980, 605), fill="#f28ab4")
            draw.rectangle((1005, 25, 1260, 710), fill="#080e16")
        raster.save(source, format="JPEG")
        sources[f"https://cdn.example/{key}.jpg"] = source.getvalue()
    requests = []
    decisions = []
    uploads = []

    def serve(request, *, mode, **_kwargs):
        requests.append(request.full_url)
        assert mode == "enforce"
        response = io.BytesIO(sources[request.full_url])
        response.headers = Message()
        response.headers["Content-Type"] = "image/jpeg"
        return response

    def verify(*, image_url, prompt, **_kwargs):
        with Image.open(io.BytesIO(base64.b64decode(image_url.partition(",")[2]))) as decoded:
            assert decoded.format == "WEBP"
        subject = next(line for line in prompt.splitlines() if line.startswith("Expected subject: "))
        decisions.append(subject)
        approved = mode == "accept_inline" and subject in {f"Expected subject: {ALPHA}", f"Expected subject: {BETA}"}
        return {"status": "MATCH" if approved else "UNRELATED", "confidence": 0.99, "visual_type": "screenshot"}

    monkeypatch.setattr(downloader, "urlopen_remote", serve)
    monkeypatch.setattr(vision_gate, "_call_vision", verify)
    monkeypatch.setattr(
        "unicornio_editor.media.visual_identity.compare_visual_assets",
        lambda *_a, **_kw: vision_gate.VisualComparison("DIFFERENT", 0.99),
    )
    monkeypatch.setattr(visual_hash, "image_hashes", lambda *_a, **_kw: {})
    monkeypatch.setattr("unicornio_editor.workflow.find_cached_game_trailer_with_status",
                        lambda *_a, **_kw: (None, "search_failed"))
    def upload(_client, path, evidence):
        assert mode == "accept_inline", "an unapproved image reached the uploader"
        with Image.open(path) as image:
            assert image.format == "WEBP"
        media_id = 90 + len(uploads)
        uploads.append(media_id)
        return {"id": media_id, "source_url": f"https://wp.test/fixture-{media_id}.webp"}

    monkeypatch.setattr("unicornio_editor.workflow.upload_image", upload)
    # The authenticated WP readback has its own disposable E2E, not this fake.
    monkeypatch.setattr("unicornio_editor.workflow._verified_v1_progress_asset", lambda *_a: True)

    code, report = invoke(monkeypatch, capsys, config, client, tmp_path, "apply-batch", str(output))
    assert code == 0, report
    row = report["posts"][0]
    if mode == "tamper":
        assert row["status"] == "preflight_blocked", row
        assert row["error_code"] == "multi_game_media_targets_unbound"
        assert requests == decisions == []
        assert not client.posts[1].updated
    else:
        assert sorted(decisions) == sorted([
            f"Expected subject: {ALPHA}", f"Expected subject: {BETA}",
            f"Expected subject: {ALPHA}; {BETA}",
        ]), row.get("media_plan_results", row)
        assert sorted(requests) == sorted([
            "https://cdn.example/alpha.jpg", "https://cdn.example/beta.jpg", "https://cdn.example/cover.jpg",
        ])
        assert row["status"] != "ready"
        assert not client.get_post(1)["meta"].get("_hermes_ready_hash")
        if mode == "accept_inline":
            from unicornio_editor.media.inserter import insert_media

            inline_assets = [
                asset for asset in row["media_plan_results"] if asset.get("status") == "accepted"
            ]
            assert [asset["subject"] for asset in inline_assets] == [ALPHA, BETA]
            assert [asset["vision_evidence"]["subject"] for asset in inline_assets] == [ALPHA, BETA]
            assert len(set(uploads)) == len(uploads) == 2
            assert row["status"] == "partial"
            assert "trailer_youtube" not in row.get("blocked_reasons", [])
            assert "fixture-90.webp" not in client.get_post(1)["content"]["raw"]
            assert "fixture-91.webp" not in client.get_post(1)["content"]["raw"]
            allowed = {"paragraph_index", "placement", "media_url", "alt_text", "credit_text", "width", "height"}
            assembled = insert_media(ORDINARY, [{k: a[k] for k in allowed if k in a} for a in inline_assets])
            alpha_region, beta_region = assembled.split(f"<h2>{BETA}: mudanças</h2>")
            assert "fixture-90.webp" in alpha_region and "fixture-91.webp" not in alpha_region
            assert "fixture-91.webp" in beta_region and "fixture-90.webp" not in beta_region


@pytest.mark.parametrize(("retry_path", "ids"), [("fresh", [1, 2]), ("cached", [1, 2]), ("fresh", [1])])
def test_expired_429_completes_canonical_batch_when_media_has_a_local_block(
    tmp_path, monkeypatch, capsys, retry_path, ids,
):
    from urllib.error import HTTPError

    from unicornio_editor import editorial_provider
    from unicornio_editor.retry_policy import editorial_backoff_status

    config = Config(
        "wordpress", "http://wp.test", "/wp-json/wp/v2", media_source_policy="credit_only",
        editorial_provider="openai", editorial_api_key="fixture", editorial_model="fixture",
    )
    client = Client([post(1, AMBIGUOUS), post(2, PARAGRAPH * 8)])
    prepared = prepare_batch(client, config, tmp_path, ids)
    source = Path(prepared["editorial_input"])
    output = source.with_name("editorial.output.json")
    sidecar = output.with_name("editorial.output.json.integrity.json")
    batch_id = prepared["batch_id"]
    answers = [result(1, AMBIGUOUS, f"{ALPHA}; {BETA}")]
    if 2 in ids:
        answers.append(result(2, PARAGRAPH * 8, ALPHA))
    network = []

    def fake_provider(request, **_kwargs):
        network.append(request)
        if len(network) == 1:
            headers = Message()
            headers["Retry-After"] = "0"
            raise HTTPError("https://fixture.example", 429, "Too Many Requests", headers, io.BytesIO(b""))
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps({
            "batch_id": batch_id, "results": answers,
        })}}]}).encode())

    monkeypatch.setattr(editorial_provider, "urlopen", fake_provider)
    code, rate_limit = invoke(monkeypatch, capsys, config, client, tmp_path, "editorial-generate-batch", str(source))
    assert code == 2 and rate_limit["error_code"] == "rate_limited", rate_limit
    assert rate_limit["resume_batch_id"] == batch_id
    assert editorial_backoff_status(tmp_path)["status"] == "expired"

    if retry_path == "cached":
        # Fresh output and sidecar are written before planning. Simulate an
        # actual required-media artifact write failure, not a local H2 error.
        from unicornio_editor import batch

        write_json = batch._write_json
        with monkeypatch.context() as isolated:
            def fail_media_write(path, value):
                if path.name == "media.input.json":
                    raise OSError("controlled media input write failure")
                return write_json(path, value)
            isolated.setattr(batch, "_write_json", fail_media_write)
            code, failure = invoke(isolated, capsys, config, client, tmp_path, "editorial-generate-batch", str(source))
            assert code == 2 and "controlled media input write failure" in failure["error"], failure
        assert editorial_backoff_status(tmp_path)["status"] == "expired"
        before = output.read_bytes(), sidecar.read_bytes()

    code, success = invoke(monkeypatch, capsys, config, client, tmp_path, "editorial-generate-batch", str(source))
    assert code == 0, success
    assert success["provider_requests"] == (1 if retry_path == "fresh" else 0), success
    assert success["ok"] == len(ids) and success["needs_retry"] == 0
    assert success["media_input"]["blocked_posts"][0]["post_id"] == 1
    assert success["media_input"]["status"] == ("required" if 2 in ids else "blocked")
    assert [item["post_id"] for item in success["media_input"]["posts"]] == ([2] if 2 in ids else [])
    assert len(network) == 2
    assert editorial_backoff_status(tmp_path)["status"] == "none"
    if retry_path == "cached":
        assert before == (output.read_bytes(), sidecar.read_bytes())


@pytest.mark.parametrize(("source_html", "body", "error"), [
    (PARAGRAPH * 8, ORDINARY, "multi_game_media_targets_unbound"),
    (ORDINARY, PARAGRAPH * 2 + f"<h2>{ALPHA}</h2><p>Curto.</p><h2>{BETA}</h2>" + PARAGRAPH * 4,
     "multi_game_media_targets_unbound"),
    (ORDINARY, PARAGRAPH * 2 + f"<h2>{ALPHA}</h2>" + PARAGRAPH + f"<h2>{BETA}</h2>" + PARAGRAPH,
     "multi_game_media_slots_unavailable"),
])
def test_ordinary_body_requires_original_game_names_substantive_sections_and_spaced_slots(
    tmp_path, source_html, body, error,
):
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", media_source_policy="credit_only")
    original = post(1, source_html)
    original["title"] = {"raw": "Notícias de videogame"}
    client = Client([original])
    prepared = prepare_batch(client, config, tmp_path, [1])
    projection = build_media_resolve_batch_input({
        "batch_id": prepared["batch_id"], "results": [result(1, body, f"{ALPHA}; {BETA}")],
    }, tmp_path, media_source_policy="credit_only")
    assert projection["status"] == "blocked", projection
    assert projection["blocked_posts"][0]["error_code"] == error


def test_retained_ordinary_game_proof_stays_bound_to_the_same_work_after_reflow():
    from unicornio_editor.workflow import _v1_media_subject, place_v1_retained_media

    overturned = (
        PARAGRAPH * 2 + f"<h2>{BETA} agora vem primeiro</h2>" + PARAGRAPH * 4
        + f"<h2>{ALPHA} agora vem depois</h2>" + PARAGRAPH * 3
    )
    editorial = payload(overturned, f"{ALPHA}; {BETA}")
    progress = {
        "subject": list(_v1_media_subject(editorial)), "post_id": 1,
        "rows": [{
            "media_url": "https://wp.test/beta.webp", "media_id": 20,
            "item_identity": "game-section-2", "target_id": "game-section-5",
            "paragraph_index": 5, "subject": BETA, "featured": False,
            "vision_evidence": {"subject": BETA, "image_role": "inline", "verdict": "accept"},
        }, {
            "media_url": "https://wp.test/unbound.webp", "media_id": 21, "featured": False,
            "vision_evidence": {"subject": f"{ALPHA}; {BETA}", "image_role": "inline", "verdict": "accept"},
        }],
    }
    retained, _ = place_v1_retained_media(progress, editorial, title="Notícias dos dois jogos", categories=[])
    assert [row["media_id"] for row in retained] == [20]
    assert retained[0]["paragraph_index"] < 6
    assert retained[0]["section_heading"].startswith(BETA)
    assert retained[0]["subject"] == BETA


def test_numbered_h3_list_targets_continue_under_their_actual_group(tmp_path):
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", media_source_policy="credit_only")
    body = f"<h2>Os jogos da semana</h2><h3>1. {ALPHA}: começo</h3>{PARAGRAPH}<h3>2. {BETA}: final</h3>{PARAGRAPH}"
    original = post(1, body)
    original["title"] = {"raw": "2 jogos para acompanhar"}
    client = Client([original])
    prepared = prepare_batch(client, config, tmp_path, [1])
    projection = build_media_resolve_batch_input({
        "batch_id": prepared["batch_id"], "results": [result(1, body, f"{ALPHA}; {BETA}")],
    }, tmp_path, media_source_policy="credit_only")
    assert projection["status"] == "required", projection
    inline = [target for target in projection["posts"][0]["targets"] if target["role"] == "inline"]
    assert [target["item_number"] for target in inline] == [1, 2]
    assert [target["item_group"] for target in inline] == ["Os jogos da semana"] * 2
    assert [target["subject"] for target in inline] == [f"{ALPHA}: começo", f"{BETA}: final"]


def test_nested_game_target_boundaries_do_not_leak_into_unrelated_sibling_containers():
    from unicornio_editor.media.inserter import ordinary_game_media_slots

    body = (
        "<main>" + PARAGRAPH * 2
        + f"<section><div><h2>{ALPHA}</h2>{PARAGRAPH}</div></section>"
        + f"<section class='unrelated'>{PARAGRAPH * 5}</section>"
        + f"<section><div><h2>{BETA}</h2>{PARAGRAPH * 5}</div></section></main>"
    )
    slots = ordinary_game_media_slots(body, [ALPHA, BETA], source_html=body, retained_slots=(2,))
    assert slots and all(subject == BETA and index >= 8 for index, (subject, _) in slots.items())


def test_retained_game_allocation_does_not_depend_on_historic_attachment_order():
    from unicornio_editor.workflow import _v1_media_subject, place_v1_retained_media

    body = (
        PARAGRAPH * 3 + f"<h2>{BETA}</h2>{PARAGRAPH}"
        + f"<h2>{ALPHA}</h2>{PARAGRAPH * 5}"
    )
    editorial = payload(body, f"{ALPHA}; {BETA}")
    rows = [{
        "media_id": number, "media_url": f"https://wp.test/{number}.webp",
        "vision_evidence": {"subject": subject, "image_role": "inline", "verdict": "accept"},
    } for number, subject in ((20, ALPHA), (21, BETA))]
    retained, _ = place_v1_retained_media(
        {"post_id": 1, "subject": list(_v1_media_subject(editorial)), "rows": rows},
        editorial, title="Jogos desta semana", categories=[],
    )
    assert [(row["media_id"], row["paragraph_index"]) for row in retained] == [(21, 3), (20, 6)]


@pytest.mark.parametrize("numbers", [(1, 3), (2, 2)])
def test_invalid_multi_game_numbered_list_does_not_abort_valid_sibling(tmp_path, numbers):
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", media_source_policy="credit_only")
    body = "".join(f"<h2>{number}. {name}: detalhes</h2>{PARAGRAPH}" for number, name in zip(numbers, (ALPHA, BETA)))
    original = post(1, body)
    original["title"] = {"raw": f"2 jogos: {ALPHA} e {BETA}"}
    client = Client([original, post(2, PARAGRAPH * 8)])
    prepared = prepare_batch(client, config, tmp_path, [1, 2])
    projection = build_media_resolve_batch_input({
        "batch_id": prepared["batch_id"],
        "results": [result(1, body, f"{ALPHA}; {BETA}"), result(2, PARAGRAPH * 8, ALPHA)],
    }, tmp_path, media_source_policy="credit_only")
    assert projection["status"] == "required", projection
    assert [target["post_id"] for target in projection["posts"]] == [2]
    assert projection["blocked_posts"][0]["error_code"] == "multi_game_media_targets_unbound"


@pytest.mark.parametrize(("metadata", "existing_reason"), [
    ({"_hermes_work_state": "wrong JSON"}, "V2 state requires the V2 executor"),
    ({"_hermes_state": "blocked", "_hermes_next_retry_at": "2999-01-01T00:00:00+00:00"},
     "v1_blocked_not_eligible"),
])
def test_layout_classifier_cannot_relabel_v2_or_cooldown(
    tmp_path, monkeypatch, capsys, metadata, existing_reason,
):
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", media_source_policy="credit_only")
    protected = post(1, AMBIGUOUS)
    protected["meta"].update(metadata)
    client = Client([protected])
    output = tmp_path / "editorial.output.json"
    output.write_text(json.dumps({
        "schema_version": 1, "batch_id": "protected", "results": [
            result(1, AMBIGUOUS, f"{ALPHA}; {BETA}"),
        ],
    }))
    code, report = invoke(monkeypatch, capsys, config, client, tmp_path, "apply-batch", str(output))
    assert code == 0, report
    assert report["posts"][0]["status"] == "preflight_blocked"
    assert existing_reason in report["posts"][0]["reason"]
    assert "error_code" not in report["posts"][0]
    assert not client.posts[1].updated
