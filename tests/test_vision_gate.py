import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from unicornio_editor.media.vision_gate import (
    VisionGateError,
    verify_image_subject,
    vision_config_ready,
)


class VisionHandler(BaseHTTPRequestHandler):
    answer = "SIM"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.last_payload = body
        payload = {"choices": [{"message": {"content": self.answer}}]}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


class VisionGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), VisionHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        VisionHandler.answer = "SIM"

    def _verify(self, **overrides):
        kwargs = {
            "image_url": "https://media.example/redfall.webp",
            "subject": "Redfall key art",
            "api_key": "test-key",
            "base_url": self.base,
            "model": "vision-test",
        }
        kwargs.update(overrides)
        return verify_image_subject(**kwargs)

    def test_confirms_subject(self):
        ok, reason = self._verify()
        self.assertTrue(ok, reason)

    def test_rejects_when_model_denies(self):
        VisionHandler.answer = "NÃO"
        ok, reason = self._verify()
        self.assertFalse(ok)
        self.assertIn("NEGOU", reason)

    def test_fails_closed_without_api_key(self):
        with self.assertRaises(VisionGateError):
            self._verify(api_key="")

    def test_fails_closed_on_inconclusive_answer(self):
        VisionHandler.answer = "talvez"
        with self.assertRaises(VisionGateError):
            self._verify()

    def test_fails_closed_on_missing_subject(self):
        with self.assertRaises(VisionGateError):
            self._verify(subject="   ")

    def test_sends_image_and_subject_to_model(self):
        self._verify()
        payload = self.server.last_payload
        content = payload["messages"][1]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertIn("https://media.example/redfall.webp", content[1]["image_url"]["url"])
        self.assertIn("Redfall key art", content[0]["text"])

    def test_vision_config_ready(self):
        ok, _ = vision_config_ready(enabled=False, api_key="")
        self.assertFalse(ok)
        ok, _ = vision_config_ready(enabled=True, api_key="")
        self.assertFalse(ok)
        ok, _ = vision_config_ready(enabled=True, api_key="x")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
