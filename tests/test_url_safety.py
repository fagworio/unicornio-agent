import unittest
from unittest.mock import patch

from unicornio_editor.media.url_safety import URLSafetyError, enforce_remote_url, inspect_remote_url


class URLSafetyTests(unittest.TestCase):
    def test_public_url_is_allowed(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 0))]):
            self.assertIsNone(inspect_remote_url("https://cdn.example/image.webp"))

    def test_audit_reports_private_destination_without_blocking(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
            finding = enforce_remote_url("http://internal.example/a.jpg", mode="audit")
        self.assertIn("não público", finding.reason)

    def test_enforce_blocks_private_destination(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.8", 0))]):
            with self.assertRaises(URLSafetyError):
                enforce_remote_url("http://internal.example/a.jpg", mode="enforce")
