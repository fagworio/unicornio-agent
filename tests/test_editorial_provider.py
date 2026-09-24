import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from unicornio_editor.editorial_provider import (
    _OUTPUT_SCHEMA,
    generate_editorial_batch,
)

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
        if type(self).editorial_mode == "string":
            # Structured Outputs strict entrega o contrato serializado.
            return json.dumps(_EDITORIAL)
        if type(self).editorial_mode == "invalid-string":
            return "{nao e json}"
        if type(self).editorial_mode == "trailer-no-url":
            return {**_EDITORIAL, "needs_trailer": True, "trailer_url": None}
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

    def _run(self, root, **kwargs):
        source = root / "editorial.input.json"
        source.write_text(json.dumps({
            "batch_id": "editorial-test",
            "posts": [
                {"post_id": 1, "cleaned_html": "<p>A</p>"},
                {"post_id": 2, "cleaned_html": "<p>B</p>"},
            ],
        }), encoding="utf-8")
        return generate_editorial_batch(
            source, api_key="test-key", base_url=self.base, model="editorial-test", root=root, **kwargs
        )

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
            self.assertIn("nao e JSON", payload["results"][0]["reason"])
            self.assertEqual(payload["results"][1]["status"], "needs_retry")

    def test_prompt_forbids_media_work_in_the_editorial_call(self):
        from unicornio_editor.editorial_provider import _SYSTEM_PROMPT

        self.assertIn('"media_plan": []', _SYSTEM_PROMPT)
        self.assertIn("needs_trailer=false", _SYSTEM_PROMPT)
        self.assertIn("never invent image URLs", _SYSTEM_PROMPT)

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


if __name__ == "__main__":
    unittest.main()
