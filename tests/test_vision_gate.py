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
    # Structured Outputs: a API responde um JSON puro {status, confidence, visual_type}.
    answer = '{"status": "MATCH", "confidence": 0.97, "visual_type": "key_art"}'
    calls = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.last_payload = body
        self.calls.append(body)
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
        VisionHandler.answer = '{"status": "MATCH", "confidence": 0.97, "visual_type": "key_art"}'
        VisionHandler.calls = []

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
        VisionHandler.answer = '{"status": "UNRELATED", "confidence": 0.97, "visual_type": "animal"}'
        ok, reason = self._verify()
        self.assertFalse(ok)
        self.assertIn("NEGOU", reason)

    def test_fails_closed_without_api_key(self):
        with self.assertRaises(VisionGateError):
            self._verify(api_key="")

    def test_fails_closed_on_inconclusive_answer(self):
        VisionHandler.answer = "nao-e-json"
        with self.assertRaises(VisionGateError):
            self._verify()

    def test_fails_closed_on_missing_subject(self):
        with self.assertRaises(VisionGateError):
            self._verify(subject="   ")

    def test_sends_image_and_subject_with_low_detail(self):
        self._verify()
        payload = self.server.last_payload
        content = payload["messages"][1]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["detail"], "low")
        self.assertIn("https://media.example/redfall.webp", content[1]["image_url"]["url"])
        self.assertIn("Redfall key art", content[0]["text"])
        # Prompt anti-vies: metadados sao contexto, pixels sao evidencia.
        self.assertIn("actual visual content", content[0]["text"])
        # Structured Outputs pede JSON com status/confidence/visual_type.
        self.assertEqual(payload.get("response_format", {}).get("type"), "json_object")

    def test_escalates_to_high_on_ambiguous(self):
        # Primeira chamada low -> AMBIGUOUS; allow_high -> segunda chamada high.
        VisionHandler.answer = '{"status": "AMBIGUOUS", "confidence": 0.60, "visual_type": "other"}'

        def deny_first():
            return '{"status": "AMBIGUOUS", "confidence": 0.60, "visual_type": "other"}'
        original_answer = VisionHandler.answer

        class HandlerThatHighs(VisionHandler):
            pass
        # Simula: 1a low AMBIGUOUS, 2a high MATCH.
        seq = [
            '{"status": "AMBIGUOUS", "confidence": 0.60, "visual_type": "other"}',
            '{"status": "MATCH", "confidence": 0.90, "visual_type": "key_art"}',
        ]
        old_do = VisionHandler.do_POST

        def do_post(self):
            VisionHandler.answer = seq.pop(0) if seq else '{"status": "MATCH", "confidence": 0.9, "visual_type": "key_art"}'
            old_do(self)

        VisionHandler.do_POST = do_post
        try:
            ok, reason = self._verify(detail="low", allow_high=True)
        finally:
            VisionHandler.do_POST = old_do
            VisionHandler.answer = original_answer
        self.assertTrue(ok, reason)
        # Deve ter feito 2 chamadas: low + high.
        details = [c["messages"][1]["content"][1]["image_url"].get("detail") for c in VisionHandler.calls]
        self.assertEqual(details, ["low", "high"])

    def test_inline_does_not_escalate_to_high(self):
        # allow_high=False: AMBIGUOUS nao escala -> rejeita (inconclusivo).
        VisionHandler.answer = '{"status": "AMBIGUOUS", "confidence": 0.60, "visual_type": "other"}'
        ok, reason = self._verify(detail="low", allow_high=False)
        self.assertFalse(ok)
        details = [c["messages"][1]["content"][1]["image_url"].get("detail") for c in VisionHandler.calls]
        self.assertEqual(details, ["low"])  # apenas 1 chamada

    def test_vision_config_ready(self):
        ok, _ = vision_config_ready(enabled=False, api_key="")
        self.assertFalse(ok)
        ok, _ = vision_config_ready(enabled=True, api_key="")
        self.assertFalse(ok)
        ok, _ = vision_config_ready(enabled=True, api_key="x")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
