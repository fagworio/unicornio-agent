from unicornio_editor.pipeline_v2.legacy import LegacyStateLoader
from unicornio_editor.pipeline_v2.model import BlockerCode, Phase


def test_blocked_media_meta_preserves_progress_over_mixed_text_error():
    state = LegacyStateLoader(lambda _: {"accepted_media": [{"media_id": 114908, "media_url": "u", "slot": 0}], "featured": {"status": "valid"}}).load(1, {
        "_hermes_state": "blocked",
        "_hermes_partial_kind": "inline_missing",
        "_hermes_media_required": "2",
        "_hermes_media_completed": "1",
        "_hermes_media_missing": "1",
        "_hermes_last_error": "imagens_no_corpo: faltam imagens; qualidade_texto: keyword ausente",
    })
    assert state.phase is Phase.MEDIA
    assert state.blocker is BlockerCode.INLINE_MISSING
    assert state.media.required == 2
    assert state.media.accepted == 1
    assert state.media.missing == 1
