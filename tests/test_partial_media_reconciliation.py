from unicornio_editor.pipeline_v2.legacy import from_legacy_state
from unicornio_editor.workflow import _reconcile_partial_media


def _four_image_article():
    body = " ".join(["Control Resonant explica os detalhes do jogo."] * 120)
    images = "".join(
        f'<figure><img src="https://wp.test/{i}.webp" alt="Control Resonant imagem {i}" />'
        "<figcaption>Crédito: Teste. Control Resonant.</figcaption></figure>"
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
