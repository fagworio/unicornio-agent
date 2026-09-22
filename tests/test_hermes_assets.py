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
        self.assertIn("5 posts READY", content)
        self.assertIn("liberam a vaga", content)

    def test_cron_installer_has_valid_shell_and_project_workdir(self):
        script = ROOT / "hermes" / "cron-install.sh"
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = script.read_text()
        self.assertIn("--workdir", content)
        self.assertIn("--skill", content)
        self.assertIn("unicorniohater-editor", content)
        self.assertIn("until 5 reach READY", content)
        self.assertIn("does not consume the quota", content)
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
        self.assertIn("HERMES_EDITORIAL_CRON_JOB_ID", content)
        self.assertIn("ID editorial registrado", content)

    def test_skill_assets_referenced_actually_exist(self):
        skill = (ROOT / "hermes" / "SKILL.md").read_text()
        # Referencias do skill devem existir no repo (senao o agente tenta ler
        # arquivos inexistentes e gasta chamadas).
        self.assertTrue((ROOT / "hermes" / "references" / "politica-imagens.md").is_file())
        self.assertTrue((ROOT / "hermes" / "references" / "editorial-texto.md").is_file())
        self.assertTrue((ROOT / "hermes" / "references" / "operacao.md").is_file())
        # Contrato dos comandos de economia de contexto (orcamento de sessao,
        # media-search-web compacto, draft --for-fix, telemetry --sessions).
        self.assertTrue((ROOT / "hermes" / "references" / "economia-contexto.md").is_file())
        self.assertIn("references/economia-contexto.md", skill)
        # O diagnostico referenciado pelo skill existe em scripts/.
        self.assertTrue((ROOT / "scripts" / "diagnostico.sh").is_file())
        self.assertIn("scripts/diagnostico.sh", skill)

    def test_skill_keeps_the_session_budget_rules(self):
        # O SKILL precisa lembrar o agente de que o TETO de posts tocados existe
        # (parar e parte do trabalho) e de que o checklist nunca e simplificado
        # para caber no orcamento.
        content = (ROOT / "hermes" / "SKILL.md").read_text()
        self.assertIn("EDITOR_MAX_POSTS_TOUCHED_PER_RUN", content)
        self.assertIn("session_budget_exhausted", content)
        self.assertIn("NUNCA simplifique o checklist", content)
        self.assertIn("telemetry --sessions", content)

    def test_cost_guard_also_guards_context_volume(self):
        # Dolar nao percebe regressao de contexto: o guard tambem mede requests,
        # input_tokens e os bytes devolvidos ao modelo.
        content = (ROOT / "hermes" / "cost_guard.py").read_text()
        self.assertIn("--limit-requests", content)
        self.assertIn("--limit-input-tokens", content)
        self.assertIn("--limit-context-bytes", content)
        monitor = (ROOT / "hermes" / "monitor.sh").read_text()
        self.assertIn("HERMES_EDITORIAL_WINDOW_CONTEXT_BYTES_LIMIT", monitor)

    def test_monitor_template_is_valid_shell(self):
        script = ROOT / "hermes" / "monitor.sh"
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = script.read_text()
        self.assertIn("@PROJECT_ROOT@", content)
        # O monitor precisa refletir somente a fila. Um timestamp/tick muda o
        # hash em todo polling e acorda o LLM sem que exista trabalho novo.
        self.assertNotIn("tick=", content)
        self.assertNotIn("date +%s", content)
        self.assertIn("cost_guard.py", content)

    def test_cost_guard_is_valid_python(self):
        script = ROOT / "hermes" / "cost_guard.py"
        result = subprocess.run(["python3", "-m", "py_compile", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
