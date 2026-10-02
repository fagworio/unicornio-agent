from unicornio_editor.pipeline_v2.legacy import LegacyStateLoader


def test_operational_projection_preserves_partial_manifest_assets():
    loader = LegacyStateLoader(lambda _: {"accepted_media": [{"media_id": 114908, "media_url": "https://img/u.webp", "slot": 0}], "featured": {"status": "valid"}})
    state = loader.load(114893, {
        "_hermes_state": "blocked",
        "_hermes_partial_kind": "inline_missing",
        "_hermes_media_required": "2",
        "_hermes_media_completed": "1",
        "_hermes_media_missing": "1",
        "_hermes_last_error": "imagens_no_corpo",
    })
    assert state.media.required == 2
    assert state.media.accepted == 1
    assert state.media.missing == 1
    assert state.media.inline[0].media_id == 114908
