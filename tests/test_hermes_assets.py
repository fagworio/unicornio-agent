import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class HermesAssetsTests(unittest.TestCase):
    def test_project_skill_contains_safety_invariants(self):
        content = (ROOT / "hermes" / "SKILL.md").read_text()
        self.assertIn("pending", content)
        self.assertIn("dry-run", content)
        self.assertIn("status", content)
        self.assertIn("Google Images", content)

    def test_cron_installer_has_valid_shell_and_project_workdir(self):
        script = ROOT / "hermes" / "cron-install.sh"
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = script.read_text()
        self.assertIn("--workdir", content)
        self.assertIn("--skill", content)
        self.assertIn("unicorniohater-editor", content)
        self.assertNotIn("EDITOR_DRY_RUN=false", content)


if __name__ == "__main__":
    unittest.main()
