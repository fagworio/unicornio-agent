from unicornio_editor.pipeline_v2.snapshot import expected_from_v1_snapshot


def test_v1_oracle_maps_blocked_domains_independently():
    for error, blocker, phase in [
        ("SEO: falha", "seo", "editorial"),
        ("ESTRUTURA: falha", "structure", "editorial"),
        ("imagens visão: rejeitada", "featured_vision", "media"),
        ("FEATURED VISION: rejected", "featured_vision", "media"),
        ("imagens_webp: inválida", "media_invalid", "media"),
    ]:
        snapshot = {"wp": {"meta": {"_hermes_state": "blocked", "_hermes_last_error": error}}, "manifest": {}}
        expected = expected_from_v1_snapshot(snapshot)
        assert expected["blocker"] == blocker
        assert expected["phase"] == phase
