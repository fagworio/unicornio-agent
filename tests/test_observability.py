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


if __name__ == "__main__":
    unittest.main()
