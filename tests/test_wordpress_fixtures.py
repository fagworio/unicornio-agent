import json
import unittest
from pathlib import Path


FIXTURES = Path(__file__).parent / "wordpress" / "fixtures"


class WordPressFixtureTests(unittest.TestCase):
    def test_fixture_matrix_is_local_and_pending(self):
        matrix = json.loads((FIXTURES / "scenario_matrix.json").read_text())
        self.assertGreaterEqual(len(matrix), 5)
        for scenario in matrix:
            self.assertEqual(scenario["status"], "pending")
            self.assertTrue((FIXTURES / scenario["fixture"]).exists())
            self.assertTrue(scenario["name"].startswith("local-"))

    def test_fixture_posts_do_not_contain_credentials(self):
        for path in FIXTURES.glob("*.json"):
            content = path.read_text().lower()
            self.assertNotIn("password", content)
            self.assertNotIn("token", content)
            self.assertNotIn("secret", content)


if __name__ == "__main__":
    unittest.main()
