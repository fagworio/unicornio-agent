from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from unicornio_editor import workflow as wf
from unicornio_editor.batch import build_media_resolve_batch_input
from unicornio_editor.wordpress import WordPressError
import test_workflow as fixture


def article():
    return "".join(
        f"<p>{i} O videogame mantém a exploração de cenários verticais e o combate "
        "cuidadoso em áreas distintas, com caminhos, ferramentas, inimigos e armadilhas "
        "que transformam a maneira de avançar por regiões conectadas do mapa.</p>"
        for i in range(36)
    )


class Client(fixture.FakeClient):
    def __init__(self, post):
        super().__init__(post)
        self.assets = {}
        self.results = []

    def get_media(self, media_id):
        if media_id not in self.assets:
            raise WordPressError("deleted", status_code=404, api_code="rest_post_invalid_id")
        return self.assets[media_id]

    def search_media(self, token):
        return [row for row in self.assets.values() if token in row["title"]["rendered"]]


@pytest.fixture
def case(tmp_path, monkeypatch):
    post = fixture.WorkflowTests().post()
    post["featured_media"] = 0
    post["meta"]["_hermes_state"] = "blocked"
    client = Client(post)
    config = fixture.WorkflowTests().config(False)
    editorial = {**fixture.editorial_payload(), "cleaned_html": article(), "game_name": "Hollow Knight Silksong"}
    binaries = {}

    def download(url, path, **_kwargs):
        Path(path).write_bytes(binaries[url])
        return path

    def staged(media_id, progress, *, leave_uncertain=False):
        url = f"https://wp.test/{media_id}.webp"
        binaries[url] = f"test WebP attachment {media_id}".encode()
        row = {
            "status": "accepted", "media_id": media_id, "media_url": url,
            "width": 800, "height": 450, "paragraph_index": 0,
            "alt_text": "Hollow Knight Silksong", "credit_text": "Crédito: exemplo.",
            "sha256": hashlib.sha256(binaries[url]).hexdigest(), "phash": f"{media_id:016x}",
            "visual_verification": {"decision": "INITIAL", "comparisons": []},
        }
        token = progress.begin({k: v for k, v in row.items() if k not in {"media_id", "media_url"}})
        client.assets[media_id] = {
            "id": media_id, "source_url": url, "mime_type": "image/webp",
            "title": {"rendered": f"Imagem [{token}]"},
            "media_details": {"width": 800, "height": 450},
        }
        if not leave_uncertain:
            progress.accepted({**row, "upload_token": token})
        return row

    monkeypatch.setattr(wf, "download_image", download)
    return client, config, tmp_path, post, editorial, staged, binaries


def test_mixed_retains_good_siblings_and_uses_revised_layout_for_deficit(case):
    client, config, root, post, editorial, staged, binaries = case
    progress = wf._V1MediaProgress(client, config, root, post, editorial, None)
    for media_id in (101, 102, 103):
        staged(media_id, progress)
    revised = {**editorial, "cleaned_html": "<p>Uma abertura revista sobre o videogame.</p>" + article()}
    snapshot = wf.verified_v1_media_progress(client, config, root, post)
    rows, cover = wf.place_v1_retained_media(snapshot, revised, title=wf._post_title(post), categories=())
    assert {r["media_id"] for r in rows} == {101, 102, 103}
    assert len({r["paragraph_index"] for r in rows}) == 3
    assert cover is None
    batch_id = "mixed-retry"
    directory = root / "work" / "batches" / batch_id
    directory.mkdir(parents=True)
    def prepare_media():
        context = {
            "post_id": 42, "title": wf._post_title(post), "cleaned_html": article(),
            "featured": {"action": "ok"}, "v1_media_progress": wf.verified_v1_media_progress(client, config, root, post),
        }
        (directory / "post-42.json").write_text(json.dumps(context))
        return build_media_resolve_batch_input({
            "batch_id": batch_id, "results": [{"post_id": 42, "status": "ok", "editorial": revised}],
        }, root)["posts"][0]
    assert prepare_media()["inline_deficit"] == 3
    del client.assets[101]
    assert prepare_media()["inline_deficit"] == 4
    binaries["https://wp.test/102.webp"] = b"bytes changed after approval"
    assert prepare_media()["existing_media_ids"] == [103]
    changed_subject = {**revised, "game_name": "Another Game"}
    assert wf.place_v1_retained_media(
        snapshot, changed_subject, title=wf._post_title(post), categories=(),
    ) == ([], None)
    post["content"]["raw"] += "<p>Human edit to the source article.</p>"
    assert wf.verified_v1_media_progress(client, config, root, post) is None


def test_upload_interruption_is_resolved_by_exact_token_and_bytes(case):
    client, config, root, post, editorial, staged, _ = case
    progress = wf._V1MediaProgress(client, config, root, post, editorial, None)
    staged(101, progress, leave_uncertain=True)
    journal = root / "backups/42/editorial.partial.json"
    original = journal.read_bytes()
    assert wf.verified_v1_media_progress(client, config, root, post)["rows"][0]["media_id"] == 101
    assert journal.read_bytes() == original  # prepare/planning is read-only
    saved = wf.verified_v1_media_progress(client, config, root, post, reconcile_uploads=True)
    assert not saved["intents"]
    assert json.loads(journal.read_text())["v1_media_progress"]["intents"] == {}


def test_unconfirmed_upload_stays_fenced_no_unsafe_adoption(case):
    client, config, root, post, editorial, staged, binaries = case
    progress = wf._V1MediaProgress(client, config, root, post, editorial, None)
    staged(101, progress, leave_uncertain=True)
    binaries["https://wp.test/101.webp"] = b"wrong bytes"
    with pytest.raises(wf.V1PartialResumeError, match="retained_media_upload_uncertain"):
        wf.verified_v1_media_progress(client, config, root, post, reconcile_uploads=True)
    journal = json.loads((root / "backups/42/editorial.partial.json").read_text())
    assert not journal["v1_media_progress"]["rows"]
    assert len(journal["v1_media_progress"]["intents"]) == 1


def test_transient_attachment_read_is_not_counted_as_missing(case, monkeypatch):
    client, config, root, post, editorial, staged, _ = case
    progress = wf._V1MediaProgress(client, config, root, post, editorial, None)
    staged(101, progress)
    def unavailable(_media_id):
        raise WordPressError("upstream unavailable", status_code=503)
    monkeypatch.setattr(client, "get_media", unavailable)
    with pytest.raises(wf.V1PartialResumeError, match="retained_media_read_unavailable"):
        wf.verified_v1_media_progress(client, config, root, post)
    assert len(json.loads((root / "backups/42/editorial.partial.json").read_text())["v1_media_progress"]["rows"]) == 1


def test_cleaner_preserves_named_media_receipt(case):
    import os
    import subprocess

    client, config, root, post, editorial, staged, _ = case
    progress = wf._V1MediaProgress(client, config, root, post, editorial, None)
    staged(101, progress)
    receipt = root / "backups/42/editorial.partial.json"
    expected = receipt.read_bytes()
    os.utime(receipt, (1, 1))
    disposable_snapshot = receipt.with_name("1.json")
    disposable_snapshot.write_text("{}")
    os.utime(disposable_snapshot, (1, 1))
    script = Path(__file__).resolve().parents[1] / "hermes/cleanup-cron.sh"
    subprocess.run(["bash", str(script)], env={**os.environ, "UNICORNIO_AGENT_ROOT": str(root)}, check=True)
    assert not disposable_snapshot.exists()
    assert receipt.read_bytes() == expected


def test_ready_intent_requires_exact_readback_before_cleanup(case):
    client, config, root, post, editorial, staged, _ = case
    progress = wf._V1MediaProgress(client, config, root, post, editorial, None)
    staged(101, progress)
    final_html = "<p>Conteúdo editorial confirmado.</p>"
    manifest = wf.build_ready_manifest(
        post_id=42, content=final_html, featured_media=7, seo=editorial["seo"],
        original_link=wf._original_link(post), editorial=editorial, policy_version=config.policy_version,
    )
    progress.record["ready_manifest"] = manifest
    progress.save()
    post["meta"].update(wf.build_meta(editorial["seo"]))
    post["meta"].update({
        wf.META_STATE: "ready", wf.META_READY_HASH: wf.manifest_hash(manifest),
        wf.META_READY_MANIFEST: wf.serialize_manifest(manifest),
    })
    path = root / "backups/42/editorial.partial.json"
    old = path.read_bytes()
    with pytest.raises(wf.V1PartialResumeError, match="ready_readback_mismatch"):
        wf.apply_editorial(client, config, root, 42, editorial)
    assert path.read_bytes() == old and not client.updated
    post["content"]["raw"] = final_html
    post["featured_media"] = 7
    assert wf.apply_editorial(client, config, root, 42, editorial)["status"] == "noop"
    assert not path.exists() and not client.updated


def test_confirmed_staging_finishes_ready_without_reacquisition(case):
    client, config, root, post, editorial, staged, _ = case
    progress = wf._V1MediaProgress(client, config, root, post, editorial, None)
    staged(101, progress)
    final = "<p>Conteúdo editorial confirmado em duas etapas.</p>"
    manifest = wf.build_ready_manifest(
        post_id=42, content=final, featured_media=7, seo=editorial["seo"],
        original_link=wf._original_link(post), editorial=editorial, policy_version=config.policy_version,
    )
    progress.record.update(ready_manifest=manifest, ready_staged=True)
    progress.save()
    post["content"]["raw"] = final
    post["featured_media"] = 7
    post["meta"].update(wf.build_meta(editorial["seo"]))
    assert wf.verified_v1_media_progress(client, config, root, post)["finalization_pending"]
    result = wf.apply_editorial(client, config, root, 42, editorial)
    assert result["status"] == "ready" and result["final_write_reconciled"]
    assert len(client.updated) == 1 and set(client.updated[0][1]) == {"meta"}
    assert not (root / "backups/42/editorial.partial.json").exists()
