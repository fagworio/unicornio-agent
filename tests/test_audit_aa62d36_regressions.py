import json
from unittest import mock

from unicornio_editor.media.phash_policy import (
    PHASH_DUPLICATE_MAX_DISTANCE,
    PHASH_NEAR_EXACT_MAX_DISTANCE,
    PHASH_POLICY_VERSION,
)
from unicornio_editor.media.source_verify import discover_article_source_candidates
from unicornio_editor.observability import append_telemetry, read_telemetry_summary
from unicornio_editor.session_metrics import session_metrics
from unicornio_editor.workflow import _expected_subject, _post_write_guard_signature


def test_listicle_subject_mapping_is_per_paragraph_and_ambiguous_is_unassigned():
    editorial = {
        "seo": {"title": "2 melhores jogos"},
        "cleaned_html": (
            "<h2>Recomendações</h2>"
            "<h3>1. Metroid Prime 4</h3><p>Primeiro item.</p>"
            "<h3>2. Halo Infinite</h3><p>Segundo item.</p>"
        ),
    }
    assert _expected_subject({"paragraph_index": 0}, editorial) == "Metroid Prime 4"
    assert _expected_subject({"paragraph_index": 1}, editorial) == "Halo Infinite"
    assert _expected_subject({}, editorial) == ""


def test_article_source_images_keep_their_local_listicle_subject():
    html = """<article>
      <h2>Jogos</h2>
      <h3>1. Metroid Prime 4</h3><figure><img src="/metroid.jpg" alt="Metroid Prime 4"></figure>
      <h3>2. Halo Infinite</h3><figure><img src="/halo.jpg" alt="Halo Infinite"></figure>
    </article>"""
    with mock.patch("unicornio_editor.media.source_verify._fetch", return_value=html.encode()):
        result = discover_article_source_candidates(
            "https://publisher.example/list", subjects=["Metroid Prime 4", "Halo Infinite"]
        )
    by_url = {
        row["direct_image_url"].rsplit("/", 1)[-1]: (row["subject"], row["alt_text"])
        for row in result
    }
    assert by_url == {
        "metroid.jpg": ("Metroid Prime 4", "Metroid Prime 4"),
        "halo.jpg": ("Halo Infinite", "Halo Infinite"),
    }


def test_listicle_source_asset_with_conflicting_item_alt_is_not_misattributed():
    html = """<article>
      <h2>Jogos recomendados</h2>
      <h3>1. Metroid Prime 4</h3><figure>
        <img src="/metroid-art.jpg" alt="Halo Infinite">
      </figure>
      <h3>2. Halo Infinite</h3><figure>
        <img src="/halo-art.jpg" alt="Metroid Prime 4">
      </figure>
    </article>"""
    with mock.patch("unicornio_editor.media.source_verify._fetch", return_value=html.encode()):
        result = discover_article_source_candidates(
            "https://publisher.example/list", subjects=["Metroid Prime 4", "Halo Infinite"]
        )
    assert result == []


def test_versioned_phash_policy_keeps_purpose_specific_distances():
    assert PHASH_POLICY_VERSION == "visual-identity-v1"
    assert PHASH_NEAR_EXACT_MAX_DISTANCE == 2
    assert PHASH_DUPLICATE_MAX_DISTANCE == 6


def test_write_guard_detects_wordpress_content_and_state_changes():
    original = {
        "id": 7, "status": "pending", "content": {"raw": "before"},
        "meta": {"_hermes_state": "ready"},
    }
    edited = json.loads(json.dumps(original))
    edited["content"]["raw"] = "human edit"
    assert _post_write_guard_signature(original) != _post_write_guard_signature(edited)
    changed_state = json.loads(json.dumps(original))
    changed_state["meta"]["_hermes_state"] = "blocked"
    assert _post_write_guard_signature(original) != _post_write_guard_signature(changed_state)


def test_session_metrics_includes_direct_editorial_and_vision_cost_without_double_count(tmp_path):
    context = {
        "run_source": "cron", "session_id": "session-1", "root_session_id": "session-1",
        "cron_job_id": "editorial-job",
    }
    with mock.patch("unicornio_editor.observability.run_context", return_value=context):
        append_telemetry(tmp_path, "apply_ready", post_id=7)
        append_telemetry(
            tmp_path, "editorial_model_request", input_tokens=1000,
            output_tokens=100, model_cost_usd=0.03,
        )
        append_telemetry(
            tmp_path, "vision_api_request", input_tokens=500,
            output_tokens=50, model_cost_usd=0.02,
        )
    summary = read_telemetry_summary(
        tmp_path, run_source="cron", cron_job_id="editorial-job"
    )
    hermes = {
        "cost_usd": 0.10, "prompt_tokens": 10, "output_tokens": 2,
        "total_model_tokens": 12, "requests": 1,
        "grand_total": {
            "cost_usd": 0.10, "prompt_tokens": 10, "output_tokens": 2,
            "total_model_tokens": 12, "requests": 1,
        },
        "aux": {},
    }
    with mock.patch("unicornio_editor.session_metrics.read_telemetry_summary", return_value=summary), \
            mock.patch("unicornio_editor.session_metrics._hermes_totals", return_value=hermes):
        metrics = session_metrics(tmp_path, state_db=tmp_path / "missing.db", job_id="editorial-job")
    assert metrics["direct_vision"]["cost_usd"] == 0.02
    assert metrics["direct_editorial"]["cost_usd"] == 0.03
    assert metrics["observed_grand_total"]["cost_usd"] == 0.15
    assert metrics["observed_grand_total"]["cost_partial"] is False
