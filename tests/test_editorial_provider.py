import json
import hashlib
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from io import BytesIO, StringIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen as real_urlopen
from unittest import mock

from unicornio_editor.editorial_provider import (
    _OUTPUT_SCHEMA,
    _normalize_output,
    _output_schema_for_policy,
    EditorialProviderError,
    generate_editorial_batch,
)
from unicornio_editor.retry_policy import editorial_backoff_status, record_editorial_backoff

_EDITORIAL = {
    "site_relevance": {
        "decision": "skip",
        "confidence": 0.98,
        "reason": "fora do escopo",
        "matched_topics": [],
    },
    "media_plan": [],
    "needs_trailer": False,
    "trailer_url": None,
    "game_name": None,
}


class EditorialHandler(BaseHTTPRequestHandler):
    calls = []
    editorial_mode = "dict"  # dict | string | invalid-string

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.calls.append(body)
        first = self._editorial_for_response()
        output = {
            "batch_id": "editorial-test",
            "results": [
                {
                    "post_id": 1,
                    "status": "ok",
                    "reason": "",
                    "editorial": first,
                },
                {
                    "post_id": 2,
                    "status": "needs_retry",
                    "reason": "fato inconsistente",
                    "editorial": {},
                },
            ],
        }
        response = json.dumps({
            "choices": [{"message": {"content": json.dumps(output)}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def _editorial_for_response(self):
        if type(self).editorial_mode == "process":
            return {
                **_EDITORIAL,
                "site_relevance": {
                    "decision": "process",
                    "confidence": 1.0,
                    "reason": "dentro da pauta",
                    "matched_topics": ["games"],
                },
            }
        if type(self).editorial_mode == "string":
            # Structured Outputs strict entrega o contrato serializado.
            return json.dumps(_EDITORIAL)
        if type(self).editorial_mode == "invalid-string":
            return "{nao e json}"
        if type(self).editorial_mode == "trailer-no-url":
            return {**_EDITORIAL, "needs_trailer": True, "trailer_url": None}
        if type(self).editorial_mode == "trailer-inconsistent":
            return {**_EDITORIAL, "trailer_url": "https://video.example/trailer"}
        return dict(_EDITORIAL)

    def log_message(self, *_args):
        pass


def _walk_schema(node, path="root"):
    """Yield (path, node) for every dict that declares a JSON type."""
    if isinstance(node, dict):
        if "type" in node:
            yield path, node
        for key, value in node.items():
            if key in {"properties"}:
                for name, child in value.items():
                    yield from _walk_schema(child, f"{path}.{name}")
            elif key in {"items", "additionalProperties"} and isinstance(value, dict):
                yield from _walk_schema(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _walk_schema(child, f"{path}[{index}]")


class EditorialProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), EditorialHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        EditorialHandler.calls = []
        EditorialHandler.editorial_mode = "dict"

    def test_process_without_allowed_topic_becomes_needs_retry_before_media(self):
        editorial = dict(_EDITORIAL)
        editorial["site_relevance"] = {
            "decision": "process",
            "confidence": 1.0,
            "reason": "processar",
            "matched_topics": ["celebridades"],
        }
        result = _normalize_output(
            {
                "batch_id": "batch-x",
                "results": [{"post_id": 1, "status": "ok", "editorial": editorial}],
            },
            batch_id="batch-x",
            post_ids={1},
            min_confidence=0.8,
            relevance_policies={1: {"games", "anime"}},
        )
        self.assertEqual(result["results"][0]["status"], "needs_retry")
        self.assertIn("pauta", result["results"][0]["reason"])

    def test_english_source_cannot_reuse_null_localization_fields(self):
        result = _normalize_output(
            {
                "batch_id": "batch-en",
                "results": [{
                    "post_id": 1,
                    "status": "ok",
                    "editorial": {
                        **_EDITORIAL,
                        "site_relevance": {
                            "decision": "process",
                            "confidence": 1.0,
                            "reason": "processar",
                            "matched_topics": ["games"],
                        },
                        "title": None,
                        "cleaned_html": None,
                        "seo": None,
                    },
                }],
            },
            batch_id="batch-en",
            post_ids={1},
            min_confidence=0.8,
            relevance_policies={1: {"games"}},
            localization_requirements={1: True},
        )
        assert result["results"][0]["status"] == "needs_retry"
        assert result["results"][0]["retry_kind"] == "text"
        assert result["results"][0]["failure_class"] == "editorial_schema_invalid"
        assert result["results"][0]["retryable"] is True
        assert "localizacao_pt_br_incompleta" in result["results"][0]["reason"]

    def _run(self, root, retry_mode="", **kwargs):
        source = root / "editorial.input.json"
        source.write_text(json.dumps({
            "batch_id": "editorial-test",
            "posts": [
                {"post_id": 1, "cleaned_html": "<p>A</p>", "retry_mode": retry_mode},
                {"post_id": 2, "cleaned_html": "<p>B</p>"},
            ],
        }), encoding="utf-8")
        api_key = kwargs.pop("api_key", "test-key")
        return generate_editorial_batch(
            source, api_key=api_key, base_url=self.base, model="editorial-test", root=root, **kwargs
        )

    def test_active_rate_limit_backoff_blocks_provider_call_for_same_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_editorial_backoff(
                root,
                batch_id="editorial-test",
                http_status=429,
                retry_after_seconds=300,
                failure_class="rate_limited",
            )
            headers = Message()
            headers["Retry-After"] = "90"
            rate_limited = HTTPError(
                self.base, 429, "Too Many Requests", headers, BytesIO(b"")
            )
            with mock.patch(
                "unicornio_editor.editorial_provider.urlopen", side_effect=rate_limited
            ) as provider, self.assertRaises(EditorialProviderError) as raised:
                self._run(root)

        provider.assert_not_called()
        self.assertEqual(raised.exception.failure_class, "rate_limited")
        self.assertEqual(raised.exception.http_status, 429)
        self.assertIsNotNone(raised.exception.retry_after_seconds)
        self.assertGreater(int(raised.exception.retry_after_seconds or 0), 0)

    def test_expired_rate_limit_backoff_requires_resuming_original_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_editorial_backoff(
                root,
                batch_id="prior-batch",
                http_status=429,
                retry_after_seconds=0,
                failure_class="rate_limited",
            )
            with mock.patch("unicornio_editor.editorial_provider.urlopen") as provider, self.assertRaises(
                EditorialProviderError
            ) as raised:
                self._run(root)

        provider.assert_not_called()
        self.assertEqual(raised.exception.failure_class, "batch_resume_required")
        self.assertEqual(raised.exception.resume_batch_id, "prior-batch")

    def test_cli_collision_cannot_replace_canonical_input_for_provider_429_resume(self):
        from unicornio_editor import cli
        from unicornio_editor.config import Config

        config = Config(
            "wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=True,
            editorial_provider="openai", editorial_api_key="test-key",
            editorial_base_url=self.base, editorial_model="editorial-test",
        )
        client = mock.Mock()
        client.get_post.side_effect = lambda post_id: {
            "id": post_id, "status": "pending", "title": {"raw": f"Videogame número {post_id}"},
            "content": {"raw": "<p>Texto original sobre videogame.</p>"},
            "meta": {"original_link": "https://source.test/news"}, "featured_media": 0,
        }
        headers = Message()
        headers["Retry-After"] = "0"
        rate_limited = HTTPError(self.base, 429, "Too Many Requests", headers, BytesIO(b""))
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(cli, "_carregar_env_do_projeto"), \
                mock.patch.object(cli, "load_config", return_value=config), \
                mock.patch.object(cli, "WordPressClient", return_value=client):
            root = Path(directory)

            def invoke(*args):
                output, error = StringIO(), StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    code = cli.main([*args, "--root", str(root)])
                return code, json.loads((output if code == 0 else error).getvalue())

            code, prepared = invoke("prepare-batch", "1", "2", "--batch-id", "editorial-test")
            self.assertEqual(code, 0, prepared)
            source = Path(prepared["editorial_input"])
            original = source.read_bytes()
            with mock.patch("unicornio_editor.editorial_provider.urlopen", side_effect=rate_limited) as provider:
                code, limited = invoke("editorial-generate-batch", str(source))
                provider.assert_called_once()
            self.assertEqual((code, limited["error_code"], limited["resume_batch_id"]),
                             (2, "rate_limited", prepared["batch_id"]))
            self.assertEqual(editorial_backoff_status(root)["status"], "expired")
            wp_reads = client.get_post.call_count
            marker = root / "work" / "editorial-provider-backoff.json"
            marker_before = marker.read_bytes()
            code, conflict = invoke("prepare-batch", "3", "--batch-id", limited["resume_batch_id"])
            self.assertEqual((code, conflict["error_code"]), (2, "batch_already_initialized"))
            self.assertEqual(client.get_post.call_count, wp_reads)
            self.assertEqual(marker.read_bytes(), marker_before)
            self.assertEqual(source.read_bytes(), original)

            code, resumed = invoke("editorial-generate-batch", str(source))
            self.assertEqual(code, 0, resumed)
            self.assertEqual(resumed["provider_requests"], 1)
            saved = json.loads(Path(resumed["output"]).read_bytes())
            self.assertEqual(saved["source_input_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual([row["post_id"] for row in saved["results"]], [1, 2])
            self.assertEqual(editorial_backoff_status(root)["status"], "none")
            code, reused = invoke("editorial-generate-batch", str(source))
            self.assertEqual(code, 0, reused)
            self.assertEqual(reused["provider_requests"], 0)
            self.assertEqual(len(EditorialHandler.calls), 1)

    def test_matching_cached_output_does_not_bypass_active_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run(root)
            self.assertTrue(Path(first["output"]).exists())
            record_editorial_backoff(
                root,
                batch_id="editorial-test",
                http_status=429,
                retry_after_seconds=300,
                failure_class="rate_limited",
            )
            with mock.patch(
                "unicornio_editor.editorial_provider.urlopen", wraps=real_urlopen
            ) as provider, self.assertRaises(EditorialProviderError) as raised:
                self._run(root)
            status = editorial_backoff_status(root)

        provider.assert_not_called()
        self.assertEqual(raised.exception.failure_class, "rate_limited")
        self.assertTrue(status["active"])

    def test_malformed_cached_output_id_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run(root)
            output_path = Path(first["output"])
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            payload["results"][0]["post_id"] = "not-an-id"
            output_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(EditorialProviderError) as raised:
                self._run(root)

        self.assertEqual(raised.exception.failure_class, "batch_output_invalid")

    def test_non_object_cached_result_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run(root)
            output_path = Path(first["output"])
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            payload["results"] = [None]
            output_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(EditorialProviderError) as raised:
                self._run(root)

        self.assertEqual(raised.exception.failure_class, "batch_output_invalid")

    def test_invalid_cached_output_error_does_not_disclose_destination_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private-output-path-marker"
            root.mkdir()
            first = self._run(root)
            Path(first["output"]).write_text("not-json", encoding="utf-8")
            with self.assertRaises(EditorialProviderError) as raised:
                self._run(root)

        self.assertEqual(raised.exception.failure_class, "batch_output_invalid")
        self.assertNotIn("private-output-path-marker", str(raised.exception))

    def test_concurrent_editorial_output_writes_do_not_collide(self):
        from unicornio_editor.editorial_provider import _write_json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "editorial.output.json"
            barrier = threading.Barrier(2)
            original_replace = Path.replace
            failures = []

            def synchronized_replace(source, target):
                if source.parent == root and source.name.startswith(".editorial.output.json."):
                    barrier.wait(timeout=5)
                return original_replace(source, target)

            def writer(value):
                try:
                    _write_json(output, {"value": value, "padding": "x" * 4096})
                except Exception as exc:  # noqa: BLE001 - captured for the assertion
                    failures.append(exc)

            with mock.patch.object(Path, "replace", new=synchronized_replace):
                threads = [threading.Thread(target=writer, args=(value,)) for value in (1, 2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(failures, [])
            self.assertIn(json.loads(output.read_text(encoding="utf-8"))["value"], {1, 2})

    def test_concurrent_same_batch_calls_provider_once_then_reuses_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_provider_entered = threading.Event()
            second_provider_entered = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()
            count_lock = threading.Lock()
            provider_calls = 0
            results = []
            failures = []

            def gated_urlopen(request, timeout):
                nonlocal provider_calls
                with count_lock:
                    provider_calls += 1
                    call_number = provider_calls
                if call_number == 1:
                    first_provider_entered.set()
                    if not release_first.wait(timeout=5):
                        raise TimeoutError("concurrent provider test was not released")
                else:
                    second_provider_entered.set()
                return real_urlopen(request, timeout=timeout)

            def run_batch(started=None):
                if started is not None:
                    started.set()
                try:
                    results.append(self._run(root))
                except Exception as exc:  # noqa: BLE001 - assert thread failures below
                    failures.append(exc)

            with mock.patch(
                "unicornio_editor.editorial_provider.urlopen", side_effect=gated_urlopen
            ):
                first = threading.Thread(target=run_batch)
                first.start()
                self.assertTrue(first_provider_entered.wait(timeout=5))
                second = threading.Thread(target=run_batch, args=(second_started,))
                second.start()
                self.assertTrue(second_started.wait(timeout=5))
                duplicate_request_started = second_provider_entered.wait(timeout=0.5)
                release_first.set()
                first.join(timeout=10)
                second.join(timeout=10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(duplicate_request_started)
        self.assertEqual(failures, [])
        self.assertEqual(provider_calls, 1)
        self.assertEqual(sorted(item["provider_requests"] for item in results), [0, 1])

    def test_successful_retry_clears_expired_batch_backoff(self):
        from unicornio_editor.editorial_provider import complete_editorial_backoff

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_editorial_backoff(
                root,
                batch_id="editorial-test",
                http_status=429,
                retry_after_seconds=0,
                failure_class="rate_limited",
            )

            result = self._run(root)
            state_after_generate = editorial_backoff_status(root)
            complete_editorial_backoff(root, "editorial-test")

            self.assertEqual(result["provider_requests"], 1)
            self.assertEqual(state_after_generate["status"], "expired")
            self.assertEqual(editorial_backoff_status(root)["status"], "none")
            telemetry = (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "editorial_provider_retry_completed"', telemetry)

    def test_backoff_finalization_can_be_deferred_until_cli_success(self):
        from unicornio_editor.editorial_provider import complete_editorial_backoff

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_editorial_backoff(
                root,
                batch_id="editorial-test",
                http_status=429,
                retry_after_seconds=0,
                failure_class="rate_limited",
            )

            result = self._run(root)
            state_after_generate = editorial_backoff_status(root)
            complete_editorial_backoff(root, "editorial-test")
            state_after_finalize = editorial_backoff_status(root)

        self.assertEqual(result["provider_requests"], 1)
        self.assertEqual(state_after_generate["status"], "expired")
        self.assertEqual(state_after_finalize["status"], "none")

    def test_retry_marker_remains_until_required_media_input_is_written(self):
        from unicornio_editor.batch import BatchError
        from unicornio_editor.editorial_provider import complete_editorial_backoff

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_dir = root / "work" / "batches" / "editorial-test"
            batch_dir.mkdir(parents=True)
            for post_id in (1, 2):
                (batch_dir / f"post-{post_id}.json").write_text(
                    json.dumps({"post_id": post_id}), encoding="utf-8"
                )
            record_editorial_backoff(
                root,
                batch_id="editorial-test",
                http_status=429,
                retry_after_seconds=0,
                failure_class="rate_limited",
            )
            with mock.patch(
                "unicornio_editor.editorial_provider.build_media_resolve_batch_input",
                side_effect=BatchError("media input write failed"),
            ), self.assertRaises(BatchError):
                self._run(root, emit_media_input=True)
            state = editorial_backoff_status(root)
            with mock.patch(
                "unicornio_editor.editorial_provider.urlopen", wraps=real_urlopen
            ) as provider:
                resumed = self._run(root, emit_media_input=True)
            state_after_resume = editorial_backoff_status(root)
            complete_editorial_backoff(root, "editorial-test")
            final_state = editorial_backoff_status(root)

        self.assertEqual(state["status"], "expired")
        self.assertEqual(state["batch_id"], "editorial-test")
        provider.assert_not_called()
        self.assertEqual(resumed["provider_requests"], 0)
        self.assertEqual(state_after_resume["status"], "expired")
        self.assertEqual(final_state["status"], "none")

    def test_generates_one_provider_request_and_preserves_partial_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._run(root)
            self.assertEqual(result["provider_requests"], 1)
            self.assertEqual(len(EditorialHandler.calls), 1)
            payload = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["results"][0]["status"], "ok")
            self.assertEqual(payload["results"][1]["status"], "needs_retry")
            telemetry = (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "editorial_model_request"', telemetry)

    def test_repeated_generation_reuses_matching_output_without_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run(root)
            with mock.patch(
                "unicornio_editor.editorial_provider.urlopen", wraps=real_urlopen
            ) as provider:
                second = self._run(root)
            telemetry = (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8")

        provider.assert_not_called()
        self.assertEqual(first["provider_requests"], 1)
        self.assertEqual(second["provider_requests"], 0)
        self.assertTrue(second["cached_output"])
        self.assertEqual(first["output"], second["output"])
        self.assertIn('"event": "editorial_output_reused"', telemetry)

    def test_generated_output_has_a_separate_integrity_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._run(root)
            output = Path(result["output"])
            integrity = output.with_name(f"{output.name}.integrity.json")

            self.assertTrue(integrity.is_file())
            manifest = json.loads(integrity.read_text(encoding="utf-8"))
            self.assertEqual(manifest["batch_id"], "editorial-test")
            self.assertIn("output_sha256", manifest)

    def test_modified_cached_output_fails_integrity_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run(root)
            output = Path(first["output"])
            payload = json.loads(output.read_text(encoding="utf-8"))
            payload["results"][0]["editorial"]["title"] = "Altered cached output"
            output.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch(
                "unicornio_editor.editorial_provider.urlopen", wraps=real_urlopen
            ) as provider, self.assertRaises(EditorialProviderError) as raised:
                self._run(root)

        provider.assert_not_called()
        self.assertEqual(raised.exception.failure_class, "batch_output_conflict")

    def test_matching_output_is_reused_without_provider_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run(root)

            reused = generate_editorial_batch(
                root / "editorial.input.json",
                api_key="",
                base_url="",
                model="",
                root=root,
            )

        self.assertEqual(reused["output"], first["output"])
        self.assertEqual(reused["provider_requests"], 0)

    def test_existing_output_is_not_reused_for_changed_input_with_same_batch_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run(root)
            self.assertTrue(Path(first["output"]).exists())
            input_path = root / "editorial.input.json"
            changed = json.loads(input_path.read_text(encoding="utf-8"))
            changed["posts"][0]["cleaned_html"] = "<p>Different content</p>"
            input_path.write_text(json.dumps(changed), encoding="utf-8")
            with mock.patch(
                "unicornio_editor.editorial_provider.urlopen", wraps=real_urlopen
            ) as provider, self.assertRaises(EditorialProviderError) as raised:
                generate_editorial_batch(
                    input_path,
                    api_key="k",
                    base_url=self.base,
                    model="editorial-test",
                    root=root,
                )

        provider.assert_not_called()
        self.assertEqual(raised.exception.failure_class, "batch_output_conflict")

    def test_invalid_input_error_does_not_disclose_local_path_or_parser_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            private_dir = Path(directory) / "private-input-path-marker"
            private_dir.mkdir()
            input_path = private_dir / "editorial.input.json"
            input_path.write_text('{"secret-ish":"bad-json"', encoding="utf-8")

            with self.assertRaises(EditorialProviderError) as raised:
                generate_editorial_batch(
                    input_path,
                    api_key="k",
                    base_url=self.base,
                    model="editorial-test",
                    root=private_dir,
                )

        self.assertEqual(raised.exception.failure_class, "request_input_invalid")
        self.assertNotIn("private-input-path-marker", str(raised.exception))
        self.assertNotIn("Expecting", str(raised.exception))

    def test_input_digest_matches_the_exact_bytes_sent_to_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "editorial.input.json"
            original_payload = {
                "batch_id": "editorial-test",
                "posts": [
                    {"post_id": 1, "cleaned_html": "<p>Original source.</p>"},
                    {"post_id": 2, "cleaned_html": "<p>Second source.</p>"},
                ],
            }
            changed_payload = {
                "batch_id": "editorial-test",
                "posts": [
                    {"post_id": 1, "cleaned_html": "<p>Changed source snapshot.</p>"},
                    {"post_id": 2, "cleaned_html": "<p>Second source.</p>"},
                ],
            }
            input_path.write_text(json.dumps(original_payload), encoding="utf-8")
            changed_bytes = json.dumps(changed_payload).encode("utf-8")
            original_read_bytes = Path.read_bytes

            def snapshot_bytes(path):
                if path == input_path:
                    return changed_bytes
                return original_read_bytes(path)

            with mock.patch.object(Path, "read_bytes", new=snapshot_bytes):
                result = generate_editorial_batch(
                    input_path,
                    api_key="k",
                    base_url=self.base,
                    model="editorial-test",
                    root=root,
                )
            output_payload = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
            request_content = EditorialHandler.calls[-1]["messages"][1]["content"]

        self.assertEqual(
            output_payload["source_input_sha256"],
            hashlib.sha256(changed_bytes).hexdigest(),
        )
        self.assertIn("Changed source snapshot", request_content)
        self.assertNotIn("Original source", request_content)

    def test_v1_generation_emits_exact_media_request_from_prepared_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_dir = root / "work" / "batches" / "editorial-test"
            batch_dir.mkdir(parents=True)
            (batch_dir / "post-1.json").write_text(json.dumps({
                "post_id": 1,
                "title": "Post comum sobre videogames",
                "cleaned_html": "<p>Texto original sobre videogames.</p>",
                "entities": ["videogames"],
                "featured": {"action": "provide", "valid": False},
            }), encoding="utf-8")
            EditorialHandler.editorial_mode = "process"

            result = self._run(root, emit_media_input=True)

            self.assertEqual(result["media_input"]["status"], "required")
            payload = json.loads(Path(result["media_input"]["path"]).read_text(encoding="utf-8"))
            request = payload["posts"][0]
            self.assertEqual(request["inline_deficit"], 2)
            self.assertEqual(request["needed"], 3)
            self.assertTrue(request["featured_required"])
            self.assertEqual(request["replacement_reserve"], 2)

    @mock.patch("unicornio_editor.editorial_provider.hermes_command_available", return_value=True)
    @mock.patch("unicornio_editor.editorial_provider.call_hermes_oneshot")
    def test_auto_provider_uses_hermes_without_legacy_key(self, call_hermes, _available):
        response = {
            "batch_id": "editorial-test",
            "results": [
                {
                    "post_id": 1,
                    "status": "ok",
                    "reason": "",
                    "retry_kind": "none",
                    "editorial": _EDITORIAL,
                },
                {
                    "post_id": 2,
                    "status": "needs_retry",
                    "reason": "fato inconsistente",
                    "retry_kind": "facts",
                    "editorial": _EDITORIAL,
                },
            ],
        }
        call_hermes.return_value = (
            "```json\n" + json.dumps(response) + "\n```",
            {"input_tokens": 11, "output_tokens": 7, "model": "gpt-6-luna-900k"},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._run(
                root,
                api_key="",
                provider="auto",
                hermes_command="hermes",
                hermes_model="configured-by-hermes",
            )
            self.assertEqual(result["provider_requests"], 1)
            call_hermes.assert_called_once()
            prompt = call_hermes.call_args.args[0]
            self.assertIn('"batch_id": "editorial-test"', prompt)
            self.assertIn('"additionalProperties":false', prompt)
            telemetry = (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
            self.assertIn('"model": "gpt-6-luna-900k"', telemetry)
            self.assertIn('"model_identity_source": "provider_usage"', telemetry)

    @mock.patch("unicornio_editor.editorial_provider.hermes_command_available", return_value=False)
    def test_auto_provider_falls_back_to_legacy_key(self, _available):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(Path(directory), provider="auto", api_key="test-key")
        self.assertEqual(result["provider_requests"], 1)
        self.assertEqual(len(EditorialHandler.calls), 1)

    @mock.patch("unicornio_editor.editorial_provider.hermes_command_available", return_value=False)
    def test_explicit_hermes_never_falls_back_to_legacy_key(self, _available):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(EditorialProviderError, "explicitamente selecionado"):
                self._run(
                    Path(directory), provider="hermes", api_key="legacy-key",
                    hermes_command="hermes",
                )
        self.assertEqual(len(EditorialHandler.calls), 0)

    def test_provider_joins_retry_mode_from_input_after_model_response(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(Path(directory), retry_mode="uncertain_second_pass")
            payload = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["results"][0]["retry_mode"], "uncertain_second_pass")
            self.assertEqual(payload["results"][1]["retry_mode"], "")

    def test_request_requires_structured_output(self):
        with tempfile.TemporaryDirectory() as directory:
            self._run(Path(directory))
            request = EditorialHandler.calls[0]
            self.assertEqual(request["response_format"]["type"], "json_schema")
            self.assertTrue(request["response_format"]["json_schema"]["strict"])

    def test_output_schema_is_valid_for_openai_strict_mode(self):
        """strict=true exige additionalProperties:false em TODO objeto e que
        `required` cubra exatamente as propriedades declaradas — foi um objeto
        livre sem additionalProperties que fez a OpenAI responder HTTP 400."""
        for path, node in _walk_schema(_OUTPUT_SCHEMA):
            if node.get("type") == "object":
                self.assertIs(
                    node.get("additionalProperties"), False,
                    f"{path}: objeto sem additionalProperties:false (OpenAI rejeita com 400)",
                )
                self.assertEqual(
                    set(node.get("required") or []), set(node.get("properties") or {}),
                    f"{path}: `required` precisa listar todas as propriedades em strict mode",
                )
        editorial = _OUTPUT_SCHEMA["properties"]["results"]["items"]["properties"]["editorial"]
        self.assertEqual(editorial["type"], "object")
        self.assertIn("site_relevance", editorial["properties"])
        self.assertIn("media_plan", editorial["properties"])
        self.assertEqual(
            set(editorial["required"]), set(editorial["properties"]),
            "o editorial precisa declarar `required` completo em strict mode",
        )

    def test_media_schema_tracks_effective_source_policy(self):
        strict = _output_schema_for_policy("strict")
        credit_only = _output_schema_for_policy("credit_only")
        strict_item = strict["properties"]["results"]["items"]["properties"]["editorial"]["properties"]["media_plan"]["items"]
        credit_item = credit_only["properties"]["results"]["items"]["properties"]["editorial"]["properties"]["media_plan"]["items"]
        self.assertIn("source_page_url", strict_item["required"])
        self.assertNotIn("source_page_url", credit_item["properties"])
        for schema in (strict, credit_only):
            for path, node in _walk_schema(schema):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, path)
                    self.assertEqual(set(node.get("required") or []), set(node.get("properties") or {}), path)

    def test_accepts_editorial_serialized_as_json_string(self):
        EditorialHandler.editorial_mode = "string"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._run(root)
            payload = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["results"][0]["status"], "ok")
            self.assertEqual(payload["results"][0]["editorial"]["media_plan"], [])
            self.assertEqual(payload["results"][1]["status"], "needs_retry")

    def test_invalid_editorial_string_isolates_the_post(self):
        EditorialHandler.editorial_mode = "invalid-string"
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(Path(directory))
            payload = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["results"][0]["status"], "needs_retry")
            self.assertEqual(payload["results"][0]["failure_class"], "invalid_response")
            self.assertTrue(payload["results"][0]["retryable"])
            self.assertIn("nao e JSON", payload["results"][0]["reason"])
            self.assertEqual(payload["results"][1]["status"], "needs_retry")

    def test_provider_http_and_timeout_failures_are_classified_without_changing_retry_policy(self):
        cases = [
            (429, True),
            (401, False),
        ]
        for status, retryable in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                error = HTTPError(self.base, status, "provider error", hdrs=None, fp=None)
                with mock.patch(
                    "unicornio_editor.editorial_provider.urlopen", side_effect=error
                ), self.assertRaises(EditorialProviderError) as raised:
                    self._run(Path(directory))
                self.assertEqual(
                    raised.exception.failure_class,
                    "rate_limited" if status == 429 else "api_http_error",
                )
                self.assertEqual(raised.exception.http_status, status)
                self.assertIs(raised.exception.retryable, retryable)

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch(
                "unicornio_editor.editorial_provider.urlopen",
                side_effect=URLError(socket.timeout("timed out")),
            ), self.assertRaises(EditorialProviderError) as raised:
                self._run(Path(directory))
            self.assertEqual(raised.exception.failure_class, "timeout")
            self.assertTrue(raised.exception.retryable)

    def test_provider_429_exposes_retry_after_without_sleeping(self):
        headers = Message()
        headers["Retry-After"] = "90"
        error = HTTPError(self.base, 429, "Too Many Requests", headers, BytesIO(b""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "unicornio_editor.editorial_provider.urlopen", side_effect=error
            ), self.assertRaises(EditorialProviderError) as raised:
                self._run(root)
            backoff = editorial_backoff_status(root)
            telemetry = (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8")

        self.assertEqual(raised.exception.failure_class, "rate_limited")
        self.assertEqual(raised.exception.http_status, 429)
        self.assertEqual(raised.exception.retry_after_seconds, 90)
        self.assertTrue(backoff["active"])
        self.assertEqual(backoff["retry_after_seconds"], 90)
        self.assertEqual(backoff["batch_id"], "editorial-test")
        self.assertIn('"event": "editorial_provider_rate_limited"', telemetry)
        self.assertNotIn("<p>A</p>", telemetry)

    def test_provider_429_persists_backoff_when_root_is_omitted(self):
        headers = Message()
        headers["Retry-After"] = "45"
        error = HTTPError(self.base, 429, "Too Many Requests", headers, BytesIO(b""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "editorial.input.json"
            input_path.write_text(json.dumps({
                "batch_id": "editorial-test",
                "posts": [
                    {"post_id": 1, "cleaned_html": "<p>A</p>"},
                    {"post_id": 2, "cleaned_html": "<p>B</p>"},
                ],
            }), encoding="utf-8")
            with mock.patch(
                "unicornio_editor.editorial_provider.urlopen", side_effect=error
            ), self.assertRaises(EditorialProviderError):
                generate_editorial_batch(
                    input_path,
                    api_key="k",
                    base_url=self.base,
                    model="editorial-test",
                )
            backoff = editorial_backoff_status(root)

        self.assertTrue(backoff["active"])
        self.assertEqual(backoff["batch_id"], "editorial-test")
        self.assertEqual(backoff["retry_after_seconds"], 45)

    def test_hermes_429_persists_retry_after_for_the_original_batch(self):
        from unicornio_editor.ai_provider import AIProviderError

        error = AIProviderError(
            "Hermes recebeu HTTP 429",
            failure_class="rate_limited",
            retryable=True,
            metadata={"http_status": 429, "retry_after_seconds": 75},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "unicornio_editor.editorial_provider.hermes_command_available",
                return_value=True,
            ), mock.patch(
                "unicornio_editor.editorial_provider.call_hermes_oneshot",
                side_effect=error,
            ), self.assertRaises(EditorialProviderError) as raised:
                self._run(root, provider="hermes", hermes_command="hermes")
            backoff = editorial_backoff_status(root)

        self.assertEqual(raised.exception.failure_class, "rate_limited")
        self.assertEqual(raised.exception.retry_after_seconds, 75)
        self.assertTrue(backoff["active"])
        self.assertEqual(backoff["batch_id"], "editorial-test")

    def test_invalid_http_response_body_is_classified(self):
        with tempfile.TemporaryDirectory() as directory:
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b"not-json"
            with mock.patch(
                "unicornio_editor.editorial_provider.urlopen", return_value=response
            ), self.assertRaises(EditorialProviderError) as raised:
                self._run(Path(directory))
            self.assertEqual(raised.exception.failure_class, "invalid_response")
            self.assertTrue(raised.exception.retryable)

    def test_prompt_forbids_media_work_in_the_editorial_call(self):
        from unicornio_editor.editorial_provider import _SYSTEM_PROMPT

        self.assertIn('"media_plan": []', _SYSTEM_PROMPT)
        self.assertIn("needs_trailer=false", _SYSTEM_PROMPT)
        self.assertIn("never invent image URLs", _SYSTEM_PROMPT)
        self.assertIn("semicolon-delimited game names", _SYSTEM_PROMPT)
        self.assertIn("game_name triggers deterministic trailer lookup", _SYSTEM_PROMPT)

    def test_missing_game_identity_recovery_keeps_text_and_uses_only_original_facts(self):
        source_post = {
            "post_id": 1, "title": "Hades II: novo trailer oficial",
            "source_content": "<p>O jogo <em>Hades II</em> recebeu um trailer.</p>",
        }
        body = "<p>O artigo revisado mantém os fatos e a estrutura.</p>"
        editorial = {
            **_EDITORIAL, "title": "Hades II: detalhes do trailer", "cleaned_html": body,
            "site_relevance": {"decision": "process", "confidence": 1.0,
                               "reason": "dentro da pauta", "matched_topics": ["games"]},
            "needs_trailer": True, "trailer_url": "https://www.youtube.com/watch?v=abcDEF12345",
        }
        response = {
            "batch_id": "trailer-repair",
            "results": [{"post_id": 1, "status": "ok", "editorial": editorial},
                        {"post_id": 2, "status": "ok", "editorial": _EDITORIAL}],
        }
        response_body = {"choices": [{"message": {"content": json.dumps(response)}}]}
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "unicornio_editor.editorial_provider.urlopen", return_value=BytesIO(json.dumps(response_body).encode())
        ) as provider:
            root = Path(directory)
            source = root / "editorial.input.json"
            source.write_text(json.dumps({"batch_id": "trailer-repair", "posts": [source_post, {"post_id": 2}]}))
            result = generate_editorial_batch(
                source, api_key="test", base_url="https://provider.example/v1", model="test", root=root,
            )
            provider.assert_called_once()
            saved = json.loads(Path(result["output"]).read_text())
            first, sibling = saved["results"]
            self.assertEqual(first["status"], "ok")
            self.assertEqual(first["editorial"], {
                **editorial, "game_name": "Hades II", "needs_trailer": False, "trailer_url": None,
            })
            self.assertIn("missing_game_name_from_explicit_source", first["normalizations"])
            self.assertEqual(sibling["editorial"], _EDITORIAL)

    def test_ambiguous_or_unrelated_original_is_not_repaired_by_trailer_or_model_text(self):
        editorial = {
            **_EDITORIAL, "title": "Hades II: trailer",
            "cleaned_html": "<p>O jogo <em>Hades II</em> recebeu um trailer.</p>",
            "site_relevance": {"decision": "process", "confidence": 1.0,
                               "reason": "dentro da pauta", "matched_topics": ["games"]},
            "needs_trailer": True,
        }
        for title, original in (
            ("Hades II: trailer", "<p>Não identifica um jogo específico.</p>"),
            ("Hades II: trailer", "<!-- <p>O jogo <em>Hades II</em>.</p> -->"),
            ("PS5: trailer de apresentação", "<p>O jogo <em>Hades II</em> é citado de passagem.</p>"),
            ("Hades II: trailer", "<p>O jogo <em>Hades</em> antecedeu a sequência.</p>"),
            ("Hades II: trailer", "<p>O jogo <em>Hades II</em> e o jogo <em>Silksong</em> apareceram.</p>"),
        ):
            with self.subTest(title=title, original=original):
                result = _normalize_output(
                    {"batch_id": "trailer-x", "results": [{"post_id": 1, "status": "ok", "editorial": editorial}]},
                    batch_id="trailer-x", post_ids={1}, min_confidence=0.8,
                    source_facts={1: {"title": title, "source_content": original}},
                )["results"][0]
                self.assertEqual(result["status"], "needs_retry")
                self.assertIn("needs_trailer requires game_name", result["reason"])

    def test_hardware_without_trailer_assertion_does_not_require_game_name(self):
        editorial = {**_EDITORIAL, "site_relevance": {"decision": "process", "confidence": 1.0,
                                                       "reason": "dentro da pauta", "matched_topics": ["hardware"]}}
        row = _normalize_output(
            {"batch_id": "hardware", "results": [{"post_id": 1, "status": "ok", "editorial": editorial}]},
            batch_id="hardware", post_ids={1}, min_confidence=0.8,
            source_facts={1: {"title": "Steam Deck: revisão do hardware", "source_content": "<p>Novo aparelho.</p>"}},
        )["results"][0]
        self.assertEqual(row["status"], "ok")
        self.assertIsNone(row["editorial"]["game_name"])

    def test_trailer_without_url_is_normalized_instead_of_invalid(self):
        """Não há busca de trailer nesta chamada: needs_trailer sem URL é
        normalizado (a descoberta determinística continua no apply)."""
        EditorialHandler.editorial_mode = "trailer-no-url"
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(Path(directory))
            payload = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["results"][0]["status"], "ok")
            editorial = payload["results"][0]["editorial"]
            self.assertFalse(editorial["needs_trailer"])
            self.assertIsNone(editorial["trailer_url"])

    def test_false_trailer_with_url_is_cleared_before_strict_validation(self):
        editorial = {**_EDITORIAL, "trailer_url": "https://video.example/trailer"}
        result = _normalize_output(
            {
                "batch_id": "batch-x",
                "results": [{"post_id": 1, "status": "ok", "editorial": editorial}],
            },
            batch_id="batch-x",
            post_ids={1},
            min_confidence=0.8,
        )
        item = result["results"][0]
        self.assertEqual(item["status"], "ok")
        self.assertIsNone(item["editorial"]["trailer_url"])
        self.assertEqual(
            item["normalizations"],
            ["trailer_url_cleared_when_needs_trailer_false"],
        )
        self.assertEqual(item["editorial"]["media_plan"], _EDITORIAL["media_plan"])

    def test_trailer_normalization_is_recorded_for_audit(self):
        EditorialHandler.editorial_mode = "trailer-inconsistent"
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(Path(directory))
            payload = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
            self.assertEqual(
                payload["results"][0]["normalizations"],
                ["trailer_url_cleared_when_needs_trailer_false"],
            )
            telemetry = (Path(directory) / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "editorial_normalization"', telemetry)

    def test_valid_trailer_is_preserved_and_invalid_true_trailer_retries(self):
        valid = {
            **_EDITORIAL,
            "game_name": "Example Game",
            "needs_trailer": True,
            "trailer_url": "https://video.example/trailer",
        }
        result = _normalize_output(
            {
                "batch_id": "batch-x",
                "results": [{"post_id": 1, "status": "ok", "editorial": valid}],
            },
            batch_id="batch-x",
            post_ids={1},
            min_confidence=0.8,
        )
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["results"][0]["editorial"]["trailer_url"], valid["trailer_url"])
        self.assertEqual(result["results"][0]["normalizations"], [])

        invalid = {
            **_EDITORIAL,
            "game_name": "Example Game",
            "needs_trailer": True,
            "trailer_url": "not-a-url",
        }
        retry = _normalize_output(
            {
                "batch_id": "batch-x",
                "results": [{"post_id": 1, "status": "ok", "editorial": invalid}],
            },
            batch_id="batch-x",
            post_ids={1},
            min_confidence=0.8,
        )
        self.assertEqual(retry["results"][0]["status"], "needs_retry")
        self.assertIn("trailer_url", retry["results"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
