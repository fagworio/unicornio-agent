import io
import json
import unittest

from unicornio_editor.observability import build_processing_markers, log_event


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


if __name__ == "__main__":
    unittest.main()
