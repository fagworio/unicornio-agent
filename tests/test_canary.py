import tempfile
import unittest
from pathlib import Path

from unicornio_editor.cli import _canary_preflight
from unicornio_editor.config import Config


class _ReadOnlyWordPressClient:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.get_calls = []
        self.media_calls = []

    def get_post(self, post_id):
        self.get_calls.append(post_id)
        return {
            "id": post_id,
            "status": self.statuses.get(post_id, "pending"),
            "title": {"raw": f"Post {post_id}"},
            "featured_media": 0,
            "meta": {},
        }

    def search_media(self, term, *, per_page=10):
        self.media_calls.append((term, per_page))
        return []


def _config(**overrides):
    values = {
        "content_source": "wordpress",
        "wordpress_url": "https://example.test",
        "wordpress_api_base": "https://example.test/wp-json/wp/v2",
        "app_user": "editor",
        "app_password": "app-password",
        "editorial_api_key": "editorial-key",
        "vision_enabled": False,
        "dry_run": True,
    }
    values.update(overrides)
    return Config(**values)


class CanaryPreflightTests(unittest.TestCase):
    def test_preflight_is_read_only_and_ready_for_two_pending_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            client = _ReadOnlyWordPressClient()
            result = _canary_preflight(
                client, _config(), Path(directory), [101, 102]
            )

        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["read_only"])
        self.assertEqual(result["post_ids"], [101, 102])
        self.assertEqual([row["status"] for row in result["posts"]], ["ready", "ready"])
        self.assertEqual(client.get_calls, [101, 102])
        self.assertEqual(len(client.media_calls), 2)
        self.assertEqual(client.media_calls[0][1], 1)
        self.assertTrue(result["provider"]["wordpress_credentials_present"])
        self.assertTrue(result["provider"]["editorial_provider_configured"])

    def test_preflight_blocks_non_pending_or_missing_provider_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            client = _ReadOnlyWordPressClient({202: "draft"})
            result = _canary_preflight(
                client,
                _config(app_password="", editorial_api_key=""),
                Path(directory),
                [201, 202],
            )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["posts"][1]["status"], "not_pending")
        self.assertFalse(result["provider"]["wordpress_credentials_present"])
        self.assertFalse(result["provider"]["editorial_provider_configured"])
        self.assertIn("corrija autenticação", result["next"])


if __name__ == "__main__":
    unittest.main()
