import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from unicornio_editor.config import Config
from unicornio_editor.wordpress import SafetyError, WordPressClient


class ApiHandler(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path, dict(self.headers)))
        if self.path.startswith("/wp-json/wp/v2/posts/42"):
            self._send(200, {"id": 42, "status": "pending", "content": {"raw": "x"}})
        elif self.path.startswith("/wp-json/wp/v2/posts"):
            self._send(200, [{"id": 42, "status": "pending"}])
        else:
            self._send(404, {"message": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.__class__.requests.append(("POST", self.path, body))
        self._send(200, {"id": 42, "status": "pending", **body})

    def _send(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


class WordPressClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def setUp(self):
        ApiHandler.requests.clear()
        self.config = Config(
            content_source="wordpress",
            wordpress_url=self.base,
            wordpress_api_base="/wp-json/wp/v2",
            app_user="bot",
            app_password="secret",
            dry_run=False,
            http_timeout=5,
        )

    def test_list_pending_filters_status_locally_without_query_status(self):
        posts = WordPressClient(self.config).list_pending(per_page=2)
        self.assertEqual(posts[0]["id"], 42)
        query = parse_qs(urlparse(ApiHandler.requests[0][1]).query)
        self.assertNotIn("status", query)
        self.assertEqual(query["context"], ["edit"])
        self.assertEqual(query["per_page"], ["2"])

    def test_get_post(self):
        self.assertEqual(WordPressClient(self.config).get_post(42)["status"], "pending")

    def test_update_rejects_status_field(self):
        with self.assertRaises(SafetyError):
            WordPressClient(self.config).update_post(42, {"status": "publish"})

    def test_update_sends_payload_without_status(self):
        result = WordPressClient(self.config).update_post(42, {"content": "new"})
        self.assertEqual(result["content"], "new")
        self.assertEqual(ApiHandler.requests[-1][2], {"content": "new"})


if __name__ == "__main__":
    unittest.main()
