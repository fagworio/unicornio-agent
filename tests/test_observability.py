import io
import json
import tempfile
import unittest
from pathlib import Path

from unicornio_editor.observability import (
    append_telemetry,
    build_processing_markers,
    log_event,
    read_telemetry_summary,
    telemetry_path,
)


class ObservabilityTests(unittest.TestCase):
    def test_markers_contain_only_safe_processing_metadata(self):
        markers = build_processing_markers("process", 0.95, processed_at="2026-08-20T12:00:00Z")
        self.assertEqual(markers["_ai_editor_decision"], "process")
        # WP REST exige string para meta registrada (tipo 'string').
        self.assertEqual(markers["_ai_editor_confidence"], "0.95")
        self.assertNotIn("content", markers)
        self.assertNotIn("password", json.dumps(markers).lower())

    def test_log_event_redacts_sensitive_keys(self):
        stream = io.StringIO()
        log_event(stream, "apply_finished", post_id=42, token="hidden", duration_ms=12)
        output = json.loads(stream.getvalue())
        self.assertEqual(output["event"], "apply_finished")
        self.assertNotIn("hidden", stream.getvalue())
        self.assertNotIn("token", output)

    def test_append_and_summary_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(root, "apply_blocked", post_id=1, reason="imagens_no_corpo", missing_images=2)
            append_telemetry(root, "apply_blocked", post_id=2, reason="verificacao_origem")
            append_telemetry(root, "apply_ready", post_id=3)
            summary = read_telemetry_summary(root)
            self.assertEqual(summary["by_event"]["apply_blocked"], 2)
            self.assertEqual(summary["by_event"]["apply_ready"], 1)
            self.assertEqual(summary["by_reason"]["apply_blocked"]["imagens_no_corpo"], 1)
            self.assertEqual(summary["by_reason"]["apply_blocked"]["verificacao_origem"], 1)
            self.assertTrue((root / "work" / "telemetry.jsonl").is_file())

    def test_telemetry_summary_tolerates_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = read_telemetry_summary(Path(directory))
            self.assertEqual(summary["total_events"], 0)
            self.assertEqual(summary["by_event"], {})

    def test_telemetry_redacts_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(root, "apply_blocked", api_key="supersecret")
            raw = telemetry_path(root).read_text()
            self.assertNotIn("supersecret", raw)
            self.assertNotIn("api_key", raw)


    def test_telemetry_context_bytes_aggregation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(root, "cmd_output", command="cards", bytes=5000)
            append_telemetry(root, "cmd_output", command="cards", bytes=2000)
            append_telemetry(root, "cmd_output", command="queue", bytes=900)
            summary = read_telemetry_summary(root)
            self.assertEqual(summary["context_bytes_by_command"]["cards"], 7000)
            self.assertEqual(summary["context_bytes_by_command"]["queue"], 900)
            self.assertEqual(summary["context_bytes_total"], 7900)

    def test_telemetry_context_bytes_ignores_non_int(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(root, "cmd_output", command="cards", bytes="nao-numero")
            summary = read_telemetry_summary(root)
            self.assertNotIn("cards", summary["context_bytes_by_command"])

    def test_telemetry_aggregates_batch_identity_and_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(
                root,
                "cmd_output",
                command="prepare-batch",
                batch_id="batch-1",
                batch_size=2,
                bytes=100,
                post_id=11,
            )
            append_telemetry(
                root,
                "apply_ready",
                batch_id="batch-1",
                batch_stage="apply",
                batch_size=2,
                post_id=11,
            )
            summary = read_telemetry_summary(root)
            self.assertEqual(summary["batches"]["batch-1"]["events"], 2)
            self.assertEqual(summary["batches"]["batch-1"]["max_size"], 2)
            self.assertEqual(summary["batches"]["batch-1"]["posts"], [11])
            self.assertEqual(summary["batches"]["batch-1"]["stages"]["apply"], 1)

    def test_telemetry_can_be_filtered_to_one_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(root, "editorial_model_request", batch_id="batch-a", batch_size=2)
            append_telemetry(root, "apply_ready", batch_id="batch-a", post_id=11)
            append_telemetry(root, "editorial_model_request", batch_id="batch-b", batch_size=2)
            append_telemetry(root, "apply_ready", batch_id="batch-b", post_id=12)

            summary = read_telemetry_summary(root, batch_id="batch-a")

        self.assertEqual(summary["total_events"], 2)
        self.assertEqual(summary["by_event"]["editorial_model_request"], 1)
        self.assertNotIn("batch-b", summary["batches"])
        self.assertEqual(summary["batches"]["batch-a"]["posts"], [11])
        self.assertEqual(summary["economics"]["editorial_model_requests"], 1)

    def test_economics_distinguishes_model_vision_tools_and_external_http(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(
                root, "editorial_model_request", stage="editorial",
                input_tokens=1000, output_tokens=400, model_cost_usd=0.02,
            )
            append_telemetry(
                root, "vision_api_request", batch_size=20,
                input_tokens=800, output_tokens=20,
            )
            append_telemetry(root, "cmd_output", command="media-resolve-batch", bytes=90)
            append_telemetry(root, "media_resolve_batch", external_http_requests=6)
            summary = read_telemetry_summary(root)
            self.assertEqual(summary["economics"]["hermes_model_requests"], 0)
            self.assertEqual(summary["economics"]["editorial_model_requests"], 1)
            self.assertEqual(summary["economics"]["vision_provider_requests"], 1)
            self.assertEqual(summary["economics"]["tool_calls"], 1)
            self.assertEqual(summary["economics"]["external_http_requests"], 6)
            self.assertEqual(summary["economics"]["input_tokens"], 1800)
            self.assertEqual(summary["economics"]["output_tokens"], 420)
            self.assertEqual(summary["economics"]["model_cost_usd"], 0.02)

    def test_economics_reports_batch_density(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(root, "editorial_model_request", stage="editorial", batch_size=2)
            append_telemetry(root, "vision_api_request", batch_size=4)
            append_telemetry(root, "apply_ready", post_id=1)
            append_telemetry(root, "apply_ready", post_id=2)
            summary = read_telemetry_summary(root)
            self.assertEqual(summary["economics"]["posts_per_editorial_request"], 2.0)
            self.assertEqual(summary["economics"]["images_per_vision_request"], 4.0)
            self.assertEqual(summary["economics"]["vision_candidates_per_ready"], 2.0)

    def test_production_and_media_funnel_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            append_telemetry(root, "apply_started", post_id=1, attempt=1, first_pass=True)
            append_telemetry(
                root, "apply_ready", post_id=1, attempts=1,
                first_pass=True, duration_ms=1200,
            )
            append_telemetry(root, "apply_started", post_id=2, attempt=2, first_pass=False)
            append_telemetry(
                root, "apply_ready", post_id=2, attempts=2,
                first_pass=False, duration_ms=1800,
            )
            append_telemetry(
                root, "media_funnel", stage="source_verify", status="passed",
                source_domain="images.example",
            )
            append_telemetry(
                root, "media_funnel", stage="source_verify", status="rejected",
                source_domain="images.example",
            )
            summary = read_telemetry_summary(root)
            self.assertEqual(summary["production"]["unique_ready_posts"], 2)
            # NOME CORRETO: dos READY, quantos foram de primeira (não é taxa de
            # sucesso de primeira tentativa).
            self.assertEqual(summary["production"]["ready_first_pass_share"], 0.5)
            self.assertIn("first_pass_success_rate", summary["production"])
            self.assertIn("first_pass_attempts", summary["production"])
            self.assertEqual(summary["production"]["average_attempts_per_ready"], 1.5)
            self.assertEqual(summary["production"]["average_ready_duration_ms"], 1500)
            self.assertEqual(summary["media_funnel"]["source_verify"]["passed"], 1)
            self.assertEqual(
                summary["media_by_domain"]["images.example"]["source_verify:rejected"], 1
            )


if __name__ == "__main__":
    unittest.main()
