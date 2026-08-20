import unittest

from unicornio_editor.cli import main


class CliTests(unittest.TestCase):
    def test_help_returns_success(self):
        with self.assertRaises(SystemExit) as raised:
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
