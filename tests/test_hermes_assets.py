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

    def test_cron_installer_wires_monitor_script_and_references(self):
        script = ROOT / "hermes" / "cron-install.sh"
        content = script.read_text()
        self.assertIn("--monitor-script", content)
        self.assertIn("unicornio-editor-monitor.sh", content)
        self.assertIn("references", content)

    def test_cron_installer_is_idempotent(self):
        # Nao duplica jobs: o install DEVE editar um job existente (em vez de
        # sempre criar) e remover duplicatas deixadas por installs antigos.
        script = ROOT / "hermes" / "cron-install.sh"
        content = script.read_text()
        self.assertIn("cron edit", content)          # atualiza em vez de duplicar
        self.assertIn("cron create", content)        # cria quando nao existe
        self.assertIn("cron remove", content)        # remove duplicados
        self.assertIn("jobs.json", content)          # le o estado real dos crons
        self.assertIn("MATCH_IDS", content)

    def test_skill_assets_referenced_actually_exist(self):
        skill = (ROOT / "hermes" / "SKILL.md").read_text()
        # Referencias do skill devem existir no repo (senao o agente tenta ler
        # arquivos inexistentes e gasta chamadas).
        self.assertTrue((ROOT / "hermes" / "references" / "politica-imagens.md").is_file())
        self.assertTrue((ROOT / "hermes" / "references" / "editorial-texto.md").is_file())
        self.assertTrue((ROOT / "hermes" / "references" / "operacao.md").is_file())
        # O diagnostico referenciado pelo skill existe em scripts/.
        self.assertTrue((ROOT / "scripts" / "diagnostico.sh").is_file())
        self.assertIn("scripts/diagnostico.sh", skill)

    def test_monitor_template_is_valid_shell(self):
        script = ROOT / "hermes" / "monitor.sh"
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("@PROJECT_ROOT@", script.read_text())


if __name__ == "__main__":
    unittest.main()
