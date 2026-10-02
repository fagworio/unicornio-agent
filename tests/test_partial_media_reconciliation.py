from pathlib import Path

from unicornio_editor.config import Config
from unicornio_editor.pipeline_v2.legacy import from_legacy_state
from unicornio_editor.workflow import _reconcile_partial_media, build_cards


def _four_image_article():
    body = " ".join(["Control Resonant explica os detalhes do jogo."] * 120)
    images = "".join(
        f'[caption] <img src="https://wp.test/{i}.webp" alt="Control Resonant imagem {i}" width="800" height="450" /> Crédito: Teste. Control Resonant. [/caption]'
        for i in range(4)
    )
    return f"<p>{body}</p>{images}"


def test_existing_html_and_manifest_assets_are_counted_once():
    content = _four_image_article()
    summary, drift = _reconcile_partial_media(
        content,
        "Control Resonant recebe novidades",
        {"control", "resonant"},
        [
            {"media_id": 101, "media_url": "https://wp.test/0.webp"},
            {"media_id": 102, "media_url": "https://wp.test/1.webp"},
        ],
        stored={"required": 4, "completed": 2, "missing": 2},
    )
    assert summary["required"] == 4
    assert summary["valid"] == 4
    assert summary["missing"] == 0
    assert drift == {"stored": {"required": 4, "completed": 2, "missing": 2}, "derived": {"required": 4, "completed": 4, "missing": 0}}


def test_v2_preserves_reconciled_count_when_manifest_has_only_asset_ledger():
    state = from_legacy_state(
        {
            "state": "partial",
            "partial_kind": "inline_missing",
            "partial_required": 4,
            "partial_completed": 4,
            "partial_missing": 0,
        },
        inline_assets=[
            {"media_id": 101, "media_url": "https://wp.test/0.webp", "slot": 0},
            {"media_id": 102, "media_url": "https://wp.test/1.webp", "slot": 1},
        ],
    )
    assert state.media.required == 4
    assert state.media.accepted == 4
    assert state.media.missing == 0
    assert len(state.media.inline) == 2


def test_v2_reconciles_manifest_assets_when_state_meta_lags_after_crash():
    state = from_legacy_state(
        {
            "state": "partial",
            "partial_kind": "inline_missing",
            "partial_required": 4,
            "partial_completed": 0,
            "partial_missing": 4,
        },
        inline_assets=[
            {"media_id": 101, "media_url": "https://wp.test/0.webp", "slot": 0},
            {"media_id": 102, "media_url": "https://wp.test/1.webp", "slot": 1},
        ],
    )
    assert state.media.accepted == 2
    assert state.media.missing == 2


def test_partial_card_uses_manifest_featured_over_wordpress_featured(tmp_path: Path):
    post = {
        "id": 42,
        "status": "pending",
        "date": "2026-10-01T00:00:00",
        "title": {"raw": "Control Resonant recebe novidades"},
        "content": {"raw": "<p>Versão antiga sem imagens.</p>"},
        "featured_media": 0,
        "meta": {
            "_hermes_state": "partial",
            "_hermes_partial_kind": "inline_missing",
            "_hermes_media_required": "2",
            "_hermes_media_completed": "0",
            "_hermes_media_missing": "2",
            "_hermes_next_retry_at": "",
        },
    }

    class Client:
        def list_pending(self, **kwargs):
            return [post]

        def get_media(self, media_id):
            return {"id": media_id, "source_url": "https://wp.test/featured.webp", "title": {"rendered": "Control Resonant"}, "alt_text": "Control Resonant", "media_details": {"width": 1280, "height": 720}}

    directory = tmp_path / "backups" / "42"
    directory.mkdir(parents=True)
    (directory / "editorial.partial.json").write_text(
        '{"state":"partial","kind":"media","required":2,"completed":0,"missing":2,"accepted_media":[],"featured":{"status":"valid","media_id":456}}'
    )
    report = build_cards(Client(), Config("wordpress", "http://wp.test", "/wp-json/wp/v2"), tmp_path, per_page=1)
    card = report["cards"][0]
    assert card["wordpress_featured"]["action"] == "provide"
    assert card["working_featured"]["action"] == "ok"
    assert card["featured"]["source"] == "partial_manifest"


def test_partial_card_reads_working_draft_instead_of_old_wordpress_html(tmp_path: Path):
    post = {
        "id": 42,
        "status": "pending",
        "date": "2026-10-01T00:00:00",
        "title": {"raw": "Control Resonant recebe novidades"},
        "content": {"raw": "<p>Versão antiga sem imagens.</p>"},
        "featured_media": 7,
        "meta": {
            "_hermes_state": "partial",
            "_hermes_partial_kind": "inline_missing",
            "_hermes_media_required": "4",
            "_hermes_media_completed": "2",
            "_hermes_media_missing": "2",
            "_hermes_next_retry_at": "",
        },
    }

    class Client:
        def list_pending(self, **kwargs):
            return [post]

        def get_media(self, media_id):
            return {"id": media_id, "source_url": "https://wp.test/featured.webp", "title": {"rendered": "Control Resonant"}, "alt_text": "Control Resonant", "media_details": {"width": 1280, "height": 720}}

    directory = tmp_path / "backups" / "42"
    directory.mkdir(parents=True)
    (directory / "editorial.partial.json").write_text(
        '{"state":"partial","kind":"media","required":4,"completed":2,"missing":2,"accepted_media":['
        '{"media_id":101,"media_url":"https://wp.test/0.webp"},'
        '{"media_id":102,"media_url":"https://wp.test/1.webp"}],"featured":{"status":"valid","media_id":7}}'
    )
    (directory / "editorial.draft.json").write_text(
        __import__("json").dumps({"cleaned_html": _four_image_article()})
    )
    report = build_cards(Client(), Config("wordpress", "http://wp.test", "/wp-json/wp/v2"), tmp_path, per_page=1)
    card = report["cards"][0]
    assert card["wordpress_images"]["valid"] == 0
    assert card["working_images"]["required"] == 4
    assert card["working_images"]["valid"] == 4
    assert card["working_images"]["missing"] == 0
    assert card["fix"]["find_inline_images"] == 0
