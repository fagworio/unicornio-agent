"""V1 list layout faults are local, not invalid editorial envelopes."""

import io
import json
from pathlib import Path

import pytest

from unicornio_editor import editorial_provider
from unicornio_editor.batch import BatchError, build_media_resolve_batch_input, prepare_batch
from unicornio_editor.config import Config
from unicornio_editor.media import downloader, search, vision_gate
from test_v1_multi_game_media import ALPHA, PARAGRAPH, Client, invoke, post, result
from test_v1_visual_media_fallback import _low_edge_cover_transport


def _list_body(items):
    return "".join(f"<h2>{n}. Obra {n}: detalhes</h2>{PARAGRAPH}" for n in range(1, items + 1))


def _list_case(promised, count, *, ready=False):
    case = post(1, _list_body(count), ready=ready)
    case["title"] = {"raw": f"Top {promised} jogos para conhecer"}
    return case


@pytest.mark.parametrize(("promised", "count", "game", "ids", "use_manifest", "policy", "expected"), [
    (3, 2, None, (1, 2), True, "credit_only", "list_media_items_mismatch"),
    (3, 2, ALPHA, (2, 1), False, "credit_only", "list_media_items_mismatch"),
    (13, 13, None, (2, 1), True, "credit_only", "list_media_target_limit"),
    (13, 13, ALPHA, (1, 2), False, "credit_only", "list_media_target_limit"),
    (3, 2, None, (1,), False, "strict", "list_media_items_mismatch"),
])
def test_list_fault_is_local_through_editorial_generate_media_resolve_and_apply(
    tmp_path, monkeypatch, capsys, promised, count, game, ids, use_manifest, policy, expected,
):
    config = Config(
        "wordpress", "http://wp.test", "/wp-json/wp/v2", media_source_policy=policy,
        editorial_provider="openai", editorial_api_key="fixture", editorial_model="fixture",
        dry_run=False, vision_enabled=True, vision_api_key="fixture", vision_detail="low",
    )
    client = Client([_list_case(promised, count), post(2, PARAGRAPH * 8)])
    prepared = prepare_batch(client, config, tmp_path, ids)
    source = Path(prepared["editorial_input"])
    output = source.with_name("editorial.output.json")
    sidecar = output.with_name("editorial.output.json.integrity.json")
    answers = {1: result(1, _list_body(count), game), 2: result(2, PARAGRAPH * 8, ALPHA)}
    requests = []

    def provider(request, **_kwargs):
        requests.append(request)
        payload = {"batch_id": prepared["batch_id"], "results": [answers[i] for i in ids]}
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}).encode())

    monkeypatch.setattr(editorial_provider, "urlopen", provider)
    code, generated = invoke(monkeypatch, capsys, config, client, tmp_path, "editorial-generate-batch", str(source))
    assert code == 0, generated
    assert generated["ok"] == len(ids) and generated["needs_retry"] == 0
    assert len(requests) == 1
    projection = generated["media_input"]
    assert projection["status"] == ("required" if 2 in ids else "blocked")
    assert [row["post_id"] for row in projection["posts"]] == ([2] if 2 in ids else [])
    assert [(row["post_id"], row["error_code"]) for row in projection["blocked_posts"]] == [(1, expected)]
    if len(ids) == 1:
        assert projection["path"] is None and not source.with_name("media.input.json").exists()
        assert not source.with_name("media.manifest.json").exists()
    immutable = output.read_bytes(), sidecar.read_bytes()
    calls = []
    downloads = []
    queries_used = []

    if use_manifest:
        direct_url = "https://cdn.example/sibling.jpg"

        def discover(queries, **_kwargs):
            queries_used.extend(queries)
            return [{"query": query, "candidates": [{"direct_image_url": direct_url, "engine": "yandex"}]}
                    for query in queries]

        monkeypatch.setattr(search, "search_web_images_batch", discover)
        code, resolved = invoke(monkeypatch, capsys, config, client, tmp_path,
                                "media-resolve-batch", projection["path"], "--full")
        assert code == 0, resolved
        assert [row["post_id"] for row in resolved["posts"]] == [2]
        assert len(queries_used) == 1
        manifest = Path(resolved["audit"])
        stale = json.loads(manifest.read_bytes())
        # A stale manifest cannot claim valid external candidates for the
        # structurally blocked item, nor can its obsolete row poison the sibling.
        stale["posts"].insert(0, {"post_id": 1, "featured_required": "invalid stale row"})
        manifest.write_text(json.dumps(stale))
        transport = _low_edge_cover_transport()

        def asset_download(request, *, mode, **kwargs):
            downloads.append(request.full_url)
            assert request.full_url == direct_url and mode == "enforce"
            return transport(request, **kwargs)

        def vision(**kwargs):
            calls.append(kwargs)
            return {"status": "UNRELATED", "confidence": 0.95, "visual_type": "key_art"}

        monkeypatch.setattr(downloader, "urlopen_remote", asset_download)
        monkeypatch.setattr(vision_gate, "_call_vision", vision)
    else:
        assert not source.with_name("media.manifest.json").exists()

    monkeypatch.setattr("unicornio_editor.workflow.find_cached_game_trailer_with_status",
                        lambda *_a, **_kw: (None, "search_failed"))
    code, applied = invoke(monkeypatch, capsys, config, client, tmp_path, "apply-batch", str(output))
    assert code == 0, applied
    assert not applied["stopped"]
    assert [row["post_id"] for row in applied["posts"]] == list(ids)
    blocked = next(row for row in applied["posts"] if row["post_id"] == 1)
    assert blocked["status"] == "preflight_blocked" and blocked["error_code"] == expected
    assert not blocked["wordpress_changed"] and not client.posts[1].updated
    assert not (tmp_path / "backups/1/apply.latest.json").exists()
    assert (output.read_bytes(), sidecar.read_bytes()) == immutable
    audit = json.loads(source.with_name("apply.manifest.json").read_bytes())
    assert blocked in audit["posts"]
    if 2 in ids:
        sibling = next(row for row in applied["posts"] if row["post_id"] == 2)
        assert sibling["status"] in {"partial", "needs_rework"}, sibling
        assert (tmp_path / "backups/2/apply.latest.json").is_file()
    assert len(downloads) == len(calls) == int(use_manifest)


@pytest.mark.parametrize("mixed", [False, True])
def test_existing_ready_list_is_not_reclassified_or_written(tmp_path, monkeypatch, capsys, mixed):
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2")
    ready = _list_case(3, 2, ready=True)
    ready["id"] = 2 if mixed else 1
    client = Client([_list_case(3, 2), ready] if mixed else [ready])
    answers = [result(ready["id"], _list_body(2), None)]
    if mixed:
        answers.insert(0, result(1, _list_body(2), None))
    output = tmp_path / "editorial.output.json"
    output.write_text(json.dumps({"batch_id": "existing-ready", "results": answers}))
    code, report = invoke(monkeypatch, capsys, config, client, tmp_path, "apply-batch", str(output))
    assert code == 0 and report["posts"][-1]["status"] == "noop", report
    assert report["preflight_blocked"] == int(mixed)
    assert client.get_post(ready["id"])["meta"]["_hermes_ready_hash"] == "immutable-existing-proof"
    assert all(not entry.updated for entry in client.posts.values())


@pytest.mark.parametrize("promised,existing", [(12, False), (13, True)])
def test_cap_counts_missing_targets_not_all_existing_list_items(tmp_path, promised, existing):
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2")
    case = _list_case(promised, promised)
    body = _list_body(promised)
    if existing:
        body = body.replace(
            "</h2>", '</h2><img src="https://wp.test/obra1.webp" alt="Obra 1 detalhes">', 1,
        )
        case["content"]["raw"] = body
    client = Client([case])
    prepared = prepare_batch(client, config, tmp_path, [1])
    request = build_media_resolve_batch_input({
        "batch_id": prepared["batch_id"], "results": [result(1, body, None)],
    }, tmp_path)
    assert request["status"] == "required", request
    assert not request.get("blocked_posts")
    row = request["posts"][0]
    assert row["required_inline_images"] == promised and row["inline_deficit"] == 12
    assert sum(item["role"] == "inline" for item in row["targets"]) == 12
    assert row["targets"][0]["item_number"] == (2 if existing else 1)


def test_bad_prepared_context_is_still_fatal_even_beside_local_fault(tmp_path):
    config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2")
    client = Client([_list_case(3, 2), post(2, PARAGRAPH * 8)])
    prepared = prepare_batch(client, config, tmp_path, [1, 2])
    directory = Path(prepared["editorial_input"]).parent
    (directory / "post-2.json").write_text('{"post_id": 999}')
    with pytest.raises(BatchError, match="contexto preparado não corresponde ao post 2"):
        build_media_resolve_batch_input({
            "batch_id": prepared["batch_id"],
            "results": [result(1, _list_body(2), None), result(2, PARAGRAPH * 8, ALPHA)],
        }, tmp_path)
    assert not (directory / "media.input.json").exists()
