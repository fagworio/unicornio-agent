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
    # When False, requests carrying a `status` query parameter are rejected
    # with HTTP 400 (simulates installs that refuse status in read queries).
    allow_status_query = True

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path, dict(self.headers)))
        if self.path.startswith("/wp-json/wp/v2/posts/42"):
            self._send(200, {"id": 42, "status": "pending", "content": {"raw": "x"}})
        elif self.path.startswith("/wp-json/wp/v2/posts"):
            if not self.__class__.allow_status_query and "status" in parse_qs(
                urlparse(self.path).query
            ):
                self._send(400, {"code": "rest_invalid_param", "message": "invalid status"})
            else:
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

    def test_list_pending_queries_status_server_side(self):
        posts = WordPressClient(self.config).list_pending(per_page=2)
        self.assertEqual(posts[0]["id"], 42)
        query = parse_qs(urlparse(ApiHandler.requests[0][1]).query)
        self.assertEqual(query["status"], ["pending"])
        self.assertEqual(query["context"], ["edit"])
        self.assertEqual(query["per_page"], ["2"])

    def test_list_pending_falls_back_to_local_filter_when_status_rejected(self):
        ApiHandler.allow_status_query = False
        try:
            posts = WordPressClient(self.config).list_pending(per_page=2)
        finally:
            ApiHandler.allow_status_query = True
        self.assertEqual(posts[0]["id"], 42)
        queries = [parse_qs(urlparse(path).query) for _, path, _ in ApiHandler.requests]
        self.assertEqual(queries[0]["status"], ["pending"])  # primary attempt
        self.assertNotIn("status", queries[1])  # fallback retry without status
        self.assertEqual(len(queries), 2)

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
