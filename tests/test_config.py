import os
import unittest
from unittest.mock import patch

from unicornio_editor.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_defaults_to_dry_run(self):
        with patch.dict(os.environ, {"WORDPRESS_URL": "http://wp.test"}, clear=True):
            config = load_config()
        self.assertTrue(config.dry_run)
        self.assertFalse(config.publish_enabled)
        self.assertEqual(config.batch_limit, 2)
        self.assertEqual(config.max_posts_per_run, 2)
        self.assertEqual(config.max_source_retries, 2)
        self.assertEqual(config.min_skip_confidence, 0.90)
        self.assertEqual(config.site_topics, ())

    def test_site_topics_and_skip_confidence_parse_env(self):
        with patch.dict(
            os.environ,
            {
                "WORDPRESS_URL": "http://wp.test",
                "SITE_TOPICS": "games, anime, cultura geek, ,streaming",
                "EDITOR_MIN_SKIP_CONFIDENCE": "0.85",
            },
            clear=True,
        ):
            config = load_config()
        self.assertEqual(config.site_topics, ("games", "anime", "cultura geek", "streaming"))
        self.assertEqual(config.min_skip_confidence, 0.85)

    def test_publish_enabled_parses_env(self):
        with patch.dict(
            os.environ,
            {"WORDPRESS_URL": "http://wp.test", "PUBLISH_ENABLED": "true"},
            clear=True,
        ):
            self.assertTrue(load_config().publish_enabled)

    def test_rejects_invalid_boolean(self):
        with patch.dict(os.environ, {"EDITOR_DRY_RUN": "maybe"}, clear=True):
            with self.assertRaises(ConfigError):
                load_config()

    def test_write_mode_requires_credentials(self):
        values = {
            "EDITOR_DRY_RUN": "false",
            "WORDPRESS_URL": "http://wp.test",
        }
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaises(ConfigError):
                load_config()

    def test_password_is_not_in_repr(self):
        values = {
            "WORDPRESS_URL": "http://wp.test",
            "WORDPRESS_APP_USER": "bot",
            "WORDPRESS_APP_PASSWORD": "super-secret",
        }
        with patch.dict(os.environ, values, clear=True):
            config = load_config()
        self.assertNotIn("super-secret", repr(config))

    def test_vision_defaults_enabled_and_low_detail(self):
        with patch.dict(os.environ, {"WORDPRESS_URL": "http://wp.test"}, clear=True):
            config = load_config()
        self.assertTrue(config.vision_enabled)
        self.assertEqual(config.vision_detail, "low")
        self.assertEqual(config.vision_max_low, 12)

    def test_vision_key_falls_back_to_openai_api_key(self):
        values = {
            "WORDPRESS_URL": "http://wp.test",
            "OPENAI_API_KEY": "sk-test-openai",
        }
        with patch.dict(os.environ, values, clear=True):
            config = load_config()
        self.assertEqual(config.vision_api_key, "sk-test-openai")

    def test_vision_key_editor_overrides_openai(self):
        values = {
            "WORDPRESS_URL": "http://wp.test",
            "OPENAI_API_KEY": "sk-test-openai",
            "EDITOR_VISION_API_KEY": "sk-test-editor",
        }
        with patch.dict(os.environ, values, clear=True):
            config = load_config()
        self.assertEqual(config.vision_api_key, "sk-test-editor")

    def test_vision_can_be_disabled(self):
        values = {
            "WORDPRESS_URL": "http://wp.test",
            "EDITOR_VISION_ENABLED": "false",
        }
        with patch.dict(os.environ, values, clear=True):
            self.assertFalse(load_config().vision_enabled)


if __name__ == "__main__":
    unittest.main()
