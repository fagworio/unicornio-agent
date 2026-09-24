import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from unicornio_editor.editorial_provider import generate_editorial_batch


class EditorialHandler(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.calls.append(body)
        output = {
            "batch_id": "editorial-test",
            "results": [
                {
                    "post_id": 1,
                    "status": "ok",
                    "reason": "",
                    "editorial": {
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
                    },
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

    def log_message(self, *_args):
        pass


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

    def test_generates_one_provider_request_and_preserves_partial_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "editorial.input.json"
            source.write_text(json.dumps({
                "batch_id": "editorial-test",
                "posts": [
                    {"post_id": 1, "cleaned_html": "<p>A</p>"},
                    {"post_id": 2, "cleaned_html": "<p>B</p>"},
                ],
            }), encoding="utf-8")
            result = generate_editorial_batch(
                source,
                api_key="test-key",
                base_url=self.base,
                model="editorial-test",
                root=root,
            )
            self.assertEqual(result["provider_requests"], 1)
            self.assertEqual(len(EditorialHandler.calls), 1)
            payload = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["results"][0]["status"], "ok")
            self.assertEqual(payload["results"][1]["status"], "needs_retry")
            telemetry = (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "editorial_model_request"', telemetry)

    def test_request_requires_structured_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "editorial.input.json"
            source.write_text(json.dumps({"batch_id": "editorial-test", "posts": [{"post_id": 1}, {"post_id": 2}]}), encoding="utf-8")
            generate_editorial_batch(
                source,
                api_key="test-key",
                base_url=self.base,
                model="editorial-test",
            )
            request = EditorialHandler.calls[0]
            self.assertEqual(request["response_format"]["type"], "json_schema")
            self.assertTrue(request["response_format"]["json_schema"]["strict"])


if __name__ == "__main__":
    unittest.main()
