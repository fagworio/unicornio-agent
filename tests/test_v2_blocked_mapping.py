import pytest

from unicornio_editor.pipeline_v2.legacy import from_legacy_state
from unicornio_editor.pipeline_v2.model import BlockerCode, Phase
from unicornio_editor.pipeline_v2.scheduler import next_action


@pytest.mark.parametrize("error, blocker, phase, action", [
    ("qualidade_texto: falha", BlockerCode.TEXT_QUALITY, Phase.EDITORIAL, "regenerate_editorial"),
    ("seo: falha", BlockerCode.SEO, Phase.EDITORIAL, "regenerate_editorial"),
    ("imagens_visao: rejeitada", BlockerCode.FEATURED_VISION, Phase.MEDIA, "resolve_featured"),
    ("imagens_no_corpo: faltante", BlockerCode.INLINE_MISSING, Phase.MEDIA, "resolve_inline"),
    ("destaque: inválido", BlockerCode.FEATURED_INVALID, Phase.MEDIA, "resolve_featured"),
])
def test_blocked_v1_derives_phase_and_next_action(error, blocker, phase, action):
    state = from_legacy_state({"state": "blocked", "last_error": error})
    assert state.blocker is blocker
    assert state.phase is phase
    assert next_action(state) == action
