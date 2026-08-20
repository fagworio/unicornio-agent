import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from unicornio_editor.cli import main
from unicornio_editor.config import Config


class FakeCliClient:
    def list_pending(self, **_kwargs):
        return [{"id": 42, "status": "pending"}]


class CliTests(unittest.TestCase):
    def test_help_returns_success(self):
        with self.assertRaises(SystemExit) as raised:
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_list_pending_prints_json(self):
        output = io.StringIO()
        config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2")
        with patch("unicornio_editor.cli.load_config", return_value=config), patch(
            "unicornio_editor.cli.WordPressClient", return_value=FakeCliClient()
        ), redirect_stdout(output):
            self.assertEqual(main(["list-pending"]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["id"], 42)


if __name__ == "__main__":
    unittest.main()
