import io
import json
from contextlib import redirect_stdout
from unittest import mock

from unicornio_editor.ai_provider import AIProviderError
from unicornio_editor.cli import main
from unicornio_editor.media.hermes_discovery import (
    _usage_has_evidence,
    capability_report,
    discover_candidates,
)


def _response(candidates):
    return json.dumps({
        "schema_version": 1,
        "provider": "hermes",
        "query": "Resident Evil official game images",
        "status": "completed",
        "candidates": candidates,
        "failure_kind": None,
    })


def test_discovery_returns_source_image_pairs_as_unverified_candidates():
    caller = mock.Mock(return_value=(_response([{
        "candidate_id": "h-1",
        "source_page_url": "https://example.test/article",
        "direct_image_url": "https://cdn.example.test/key-art.jpg",
        "title": "Key art",
        "discovery_method": "web_search",
    }]), {"tool_calls": {"web_search": 1}}))

    result = discover_candidates("Resident Evil official game images", caller=caller)

    assert result["status"] == "completed"
    assert result["failure_kind"] is None
    assert result["candidates"][0]["usable"] is False
    assert result["candidates"][0]["discovery_only"] is True
    assert result["candidates"][0]["rejected_reason"] == "hermes_discovery_unverified"
    caller.assert_called_once()
    assert caller.call_args.kwargs["toolsets"] == "web"


def test_discovery_rejects_missing_or_ambiguous_urls_without_persisting_anything():
    caller = mock.Mock(return_value=(_response([
        {"candidate_id": "missing", "source_page_url": "", "direct_image_url": "https://cdn.test/a.jpg"},
        {"candidate_id": "same", "source_page_url": "https://example.test/a", "direct_image_url": "https://example.test/a"},
    ]), {}))

    result = discover_candidates("query", caller=caller)

    assert result["status"] == "failed"
    assert result["failure_kind"] == "hermes_no_results"
    assert result["candidates"] == []


def test_discovery_classifies_provider_timeout():
    error = AIProviderError("slow", failure_class="timeout")
    caller = mock.Mock(side_effect=error)

    result = discover_candidates("query", caller=caller)

    assert result["status"] == "failed"
    assert result["failure_kind"] == "hermes_search_timeout"


def test_capability_report_is_unverified_without_live_probe():
    with mock.patch("unicornio_editor.media.hermes_discovery._version", return_value={"available": True, "status": "SUPPORTED"}):
        result = capability_report(probe=False)

    assert result["status"] == "UNVERIFIED"
    assert result["read_only"] is True
    assert all(item["status"] == "UNVERIFIED" for item in result["tools"].values())
    assert result["writes"] == {"wordpress": 0, "filesystem": 0, "media_registry": 0}


def test_tool_evidence_requires_the_named_tool():
    assert not _usage_has_evidence({"tool_calls": {"vision_analyze": 1}}, "web_search")
    assert not _usage_has_evidence({"tool_calls": 3, "tool_name": "vision_analyze"}, "web_search")
    assert _usage_has_evidence({"tool_calls": [{"name": "web_search"}]}, "web_search")
    assert _usage_has_evidence({"web_extract": {"count": 1}}, "web_extract")


def test_capability_probe_separates_search_extract_and_source_preflight():
    payload = _response([{
        "candidate_id": "h-1",
        "source_page_url": "https://example.test/article",
        "direct_image_url": "https://cdn.example.test/key-art.jpg",
    }])
    with mock.patch(
        "unicornio_editor.media.hermes_discovery._version",
        return_value={"available": True, "status": "SUPPORTED"},
    ), mock.patch(
        "unicornio_editor.media.hermes_discovery.call_hermes_oneshot",
        return_value=(payload, {"tool_calls": [{"name": "web_search"}]}),
    ), mock.patch(
        "unicornio_editor.media.hermes_discovery._verify_candidate_read_only",
        return_value=(True, "source_verified"),
    ):
        result = capability_report(
            command="hermes", probe=True, queries=(("game", "query"),)
        )

    assert result["tools"]["web_search"]["real_tool_evidence"] is True
    assert result["tools"]["web_extract"]["real_tool_evidence"] is False
    assert result["tools"]["web_extract"]["status"] == "DEGRADED"
    assert result["candidate_metrics"]["source_page_preflight_verified"] == 1


def test_capability_cli_does_not_load_wordpress_configuration():
    output = io.StringIO()
    with mock.patch("unicornio_editor.cli._carregar_env_do_projeto") as load_env, redirect_stdout(output):
        exit_code = main([
            "v2-hermes-media-capabilities",
            "--command", "/path/that/does/not/exist",
        ])

    result = json.loads(output.getvalue())
    assert exit_code == 2
    assert result["status"] == "UNAVAILABLE"
    assert result["read_only"] is True
    assert result["hermes"]["available"] is False
    load_env.assert_called_once()
