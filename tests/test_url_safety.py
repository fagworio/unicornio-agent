import unittest
import os
from unittest.mock import Mock, patch
from urllib.request import Request

from unicornio_editor.media.url_safety import (
    URLSafetyError,
    URLSafetyFinding,
    _PinnedHTTPSConnection,
    _SafeRedirectHandler,
    _connect_pinned,
    enforce_remote_url,
    inspect_remote_url,
    urlopen_remote,
)


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

    def test_enforce_blocks_private_ipv6_destination(self):
        with patch(
            "socket.getaddrinfo",
            return_value=[(10, 1, 6, "", ("fd00::8", 443, 0, 0))],
        ):
            with self.assertRaises(URLSafetyError):
                enforce_remote_url("https://internal.example/a.jpg", mode="enforce")

    def test_enforce_rejects_any_private_address_in_mixed_dns_answer(self):
        with patch(
            "socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("8.8.8.8", 80)),
                (10, 1, 6, "", ("fd00::8", 80, 0, 0)),
            ],
        ):
            with self.assertRaises(URLSafetyError):
                enforce_remote_url("http://mixed.example/a.jpg", mode="enforce")

    def test_pinned_connect_uses_the_same_resolved_address(self):
        fake_socket = Mock()
        with patch(
            "unicornio_editor.media.url_safety._resolve_addresses",
            return_value=[(2, 1, 6, "", ("8.8.8.8", 80))],
        ), patch("socket.socket", return_value=fake_socket):
            result = _connect_pinned(
                "cdn.example", 80, 5, None, scheme="http", mode="enforce", audit=None
            )
        self.assertIs(result, fake_socket)
        fake_socket.connect.assert_called_once_with(("8.8.8.8", 80))

    def test_audit_mode_reports_private_ip_but_preserves_legacy_reachability(self):
        fake_socket = Mock()
        findings = []
        with patch(
            "unicornio_editor.media.url_safety._resolve_addresses",
            return_value=[(2, 1, 6, "", ("10.0.0.9", 80))],
        ), patch("socket.socket", return_value=fake_socket):
            result = _connect_pinned(
                "legacy-cdn.example", 80, 5, None,
                scheme="http", mode="audit", audit=findings.append,
            )
        self.assertIs(result, fake_socket)
        fake_socket.connect.assert_called_once_with(("10.0.0.9", 80))
        self.assertEqual(len(findings), 1)
        self.assertIn("não público", findings[0].reason)

    def test_pinned_https_preserves_original_hostname_for_tls_sni(self):
        raw_socket = Mock()
        wrapped_socket = Mock()
        context = Mock()
        context.wrap_socket.return_value = wrapped_socket
        with patch(
            "unicornio_editor.media.url_safety._connect_pinned",
            return_value=raw_socket,
        ) as connect:
            connection = _PinnedHTTPSConnection(
                "cdn.example", context=context, timeout=5
            )
            connection.connect()
        connect.assert_called_once()
        context.wrap_socket.assert_called_once_with(
            raw_socket, server_hostname="cdn.example"
        )
        self.assertIs(connection.sock, wrapped_socket)

    def test_remote_opener_refuses_proxy_that_would_bypass_dns_pinning(self):
        fake_opener = Mock()
        fake_opener.open.return_value = "opened"
        with patch.dict(os.environ, {"https_proxy": "http://proxy.internal:3128"}), \
                patch("unicornio_editor.media.url_safety.getproxies", return_value={
                    "https": "http://proxy.internal:3128"
                }), patch(
                    "unicornio_editor.media.url_safety.proxy_bypass", return_value=False
                ), patch(
                    "unicornio_editor.media.url_safety.build_opener",
                    return_value=fake_opener,
                ) as build_opener:
            with self.assertRaisesRegex(URLSafetyError, "proxy configurado"):
                urlopen_remote("https://cdn.example/image.webp", mode="enforce")
        build_opener.assert_not_called()

    def test_explicit_off_mode_preserves_urllib_proxy_behavior(self):
        fake_opener = Mock()
        fake_opener.open.return_value = "opened"
        with patch.dict(os.environ, {"https_proxy": "http://proxy.internal:3128"}), \
                patch(
                    "unicornio_editor.media.url_safety.build_opener",
                    return_value=fake_opener,
                ) as build_opener:
            result = urlopen_remote("https://cdn.example/image.webp", mode="off")
        self.assertEqual(result, "opened")
        build_opener.assert_called_once_with()
        fake_opener.open.assert_called_once()

    def test_enforced_remote_open_rejects_private_dns_before_socket_creation(self):
        with patch(
            "socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 80))],
        ), patch("unicornio_editor.media.url_safety.getproxies", return_value={}), \
                patch("socket.socket") as make_socket:
            with self.assertRaises(URLSafetyError):
                urlopen_remote("http://internal.example/image.webp", mode="enforce")
        make_socket.assert_not_called()

    def test_dns_rebinding_to_private_address_is_rejected_at_connect_time(self):
        with patch(
            "socket.getaddrinfo",
            side_effect=[
                [(2, 1, 6, "", ("8.8.8.8", 80))],
                [(10, 1, 6, "", ("fd00::8", 80, 0, 0))],
            ],
        ) as resolve, patch(
            "unicornio_editor.media.url_safety.getproxies", return_value={}
        ), patch("socket.socket") as make_socket:
            with self.assertRaises(URLSafetyError):
                urlopen_remote("http://rebind.example/image.webp", mode="enforce")
        self.assertEqual(resolve.call_count, 2)
        make_socket.assert_not_called()

    def test_redirect_target_is_revalidated_before_following(self):
        handler = _SafeRedirectHandler("enforce")
        with patch(
            "unicornio_editor.media.url_safety.enforce_remote_url",
            side_effect=URLSafetyError("private redirect target"),
        ) as enforce:
            with self.assertRaises(URLSafetyError):
                handler.redirect_request(
                    Request("https://public.example/a"),
                    Mock(),
                    302,
                    "Found",
                    {"Location": "http://internal.example/b"},
                    "http://internal.example/b",
                )
        enforce.assert_called_once_with("http://internal.example/b", mode="enforce")
