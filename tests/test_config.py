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
        self.assertEqual(config.batch_limit, 3)

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


if __name__ == "__main__":
    unittest.main()
