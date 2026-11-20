from __future__ import annotations

import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
HARNESS = ROOT / "scripts" / "editorial_localization_harness.py"


class EditorialLocalizationHarnessTests(unittest.TestCase):
    def test_failed_technical_check_keeps_expired_backoff_marker(self):
        from unicornio_editor.retry_policy import editorial_backoff_status, record_editorial_backoff

        editorial = {
            "title": "Título editorial",
            "cleaned_html": "<p>Texto editorial útil e natural.</p>",
            "seo": {
                "title": "Título editorial",
                "meta_description": "Descrição editorial suficiente para a verificação.",
                "focus_keyword": "videogame",
            },
            "site_relevance": {"decision": "process", "confidence": 0.95},
        }
        batch = {
            "batch_id": "harness-resume",
            "items": [{"post_id": 71, "status": "ok", "editorial": editorial}],
        }
        provider_result = {"batch_id": "harness-resume", "input_tokens": 0, "output_tokens": 0}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.json"
            input_path.write_text(json.dumps({
                "batch_id": "harness-resume",
                "posts": [{"post_id": 71, "localization": {"required": True}}],
            }), encoding="utf-8")
            output_dir = root / "output"
            record_editorial_backoff(
                output_dir,
                batch_id="harness-resume",
                http_status=429,
                retry_after_seconds=0,
                failure_class="rate_limited",
            )
            harness_main = runpy.run_path(str(HARNESS))["main"]
            stdout = io.StringIO()

            def generate(*_args, **kwargs):
                self.assertTrue(kwargs.get("defer_backoff_clear"))
                return provider_result

            with mock.patch.object(sys, "argv", [
                str(HARNESS), "--input", str(input_path), "--output-dir", str(output_dir)
            ]), mock.patch(
                "unicornio_editor.editorial_provider.generate_editorial_batch",
                side_effect=generate,
            ), mock.patch(
                "unicornio_editor.batch.load_editorial_batch", return_value=batch
            ), mock.patch(
                "unicornio_editor.editorial_schema.validate_editorial",
                side_effect=lambda value, **_kwargs: value,
            ), mock.patch(
                "unicornio_editor.language.editorial_language_report",
                return_value={"passed": False},
            ), mock.patch(
                "unicornio_editor.content_quality.looks_like_operational_envelope",
                return_value=False,
            ), mock.patch(
                "unicornio_editor.config.editorial_price_per_1m", return_value=(0.0, 0.0)
            ), mock.patch(
                "unicornio_editor.observability.usage_cost_usd", return_value=0.0
            ), redirect_stdout(stdout):
                exit_code = harness_main()

            state = editorial_backoff_status(output_dir)

        self.assertEqual(exit_code, 2)
        self.assertEqual(state["status"], "expired")
        self.assertEqual(state["batch_id"], "harness-resume")
        self.assertTrue(stdout.getvalue())
