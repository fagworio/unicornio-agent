import json
import os
import shutil
import subprocess
import sys
import tempfile
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
        self.assertIn("PROMPT='Execute exclusivamente o lifecycle editorial V2", content)
        self.assertIn("v2-cycle --root .", content)
        self.assertNotIn("v2-run --root . --limit 5 --preview", content)
        self.assertNotIn("Prefer one prepare-batch plus editorial-generate-batch", content)
        self.assertNotIn("then use media-resolve-batch for both posts", content)

    def test_publish_cron_is_fail_closed_when_publication_is_disabled(self):
        script = ROOT / "hermes" / "publish-cron.sh"
        content = script.read_text()
        self.assertIn("set -euo pipefail", content)
        self.assertIn("PUBLISH_ENABLED:-false", content)
        self.assertNotIn("export PUBLISH_ENABLED=true", content)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("PUBLISH_ENABLED=false\n", encoding="utf-8")
            result = subprocess.run(
                ["bash", str(script)],
                cwd=root,
                env={**os.environ, "UNICORNIO_AGENT_ROOT": str(root)},
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Publicacao desabilitada", result.stderr)

    def test_cron_installer_wires_monitor_script_and_references(self):
        script = ROOT / "hermes" / "cron-install.sh"
        content = script.read_text()
        self.assertIn("--monitor-script", content)
        self.assertIn("unicornio-editor-monitor.sh", content)
        self.assertIn("references", content)
        self.assertIn("validate-monitor.sh", content)

    def test_monitor_sync_updates_only_installed_script_with_backup(self):
        script = ROOT / "hermes" / "sync-monitor.sh"
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / "hermes"
            target_dir = hermes_home / "scripts"
            target_dir.mkdir(parents=True)
            target = target_dir / "unicornio-editor-monitor.sh"
            target.write_text("old monitor\n", encoding="utf-8")
            target.chmod(0o755)
            jobs_file = hermes_home / "cron" / "jobs.json"
            jobs_file.parent.mkdir()
            jobs_file.write_text('{"jobs": []}\n', encoding="utf-8")
            jobs_before = jobs_file.read_text(encoding="utf-8")

            completed = subprocess.run(
                ["bash", str(script)],
                cwd=ROOT,
                env={**os.environ, "HERMES_HOME": str(hermes_home)},
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(f'ROOT="{ROOT}"', target.read_text(encoding="utf-8"))
            self.assertNotIn("old monitor", target.read_text(encoding="utf-8"))
            backups = list(target_dir.glob("unicornio-editor-monitor.sh.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "old monitor\n")
            self.assertEqual(jobs_file.read_text(encoding="utf-8"), jobs_before)

    def test_monitor_sync_rejects_invalid_rendered_bash_before_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            hermes_dir = project / "hermes"
            target_dir = Path(directory) / "hermes-home" / "scripts"
            hermes_dir.mkdir(parents=True)
            target_dir.mkdir(parents=True)
            shutil.copy2(ROOT / "hermes" / "sync-monitor.sh", hermes_dir / "sync-monitor.sh")
            shutil.copy2(ROOT / "hermes" / "validate-monitor.sh", hermes_dir / "validate-monitor.sh")
            (hermes_dir / "monitor.sh").write_text(
                'ROOT="@PROJECT_ROOT@"\n'
                'if then\n'
                '  queue --monitor --root "$ROOT"\n'
                'fi\n',
                encoding="utf-8",
            )
            target = target_dir / "unicornio-editor-monitor.sh"
            target.write_text("old monitor\n", encoding="utf-8")
            target.chmod(0o755)
            completed = subprocess.run(
                ["bash", str(hermes_dir / "sync-monitor.sh")],
                env={**os.environ, "HERMES_HOME": str(target_dir.parents[1])},
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "old monitor\n")
            self.assertEqual(list(target_dir.glob("unicornio-editor-monitor.sh.bak.*")), [])

    def test_rendered_monitor_validator_rejects_unresolved_placeholder(self):
        validator = ROOT / "hermes" / "validate-monitor.sh"
        with tempfile.TemporaryDirectory() as directory:
            rendered = Path(directory) / "monitor.sh"
            rendered.write_text(
                'ROOT="@PROJECT_ROOT@"\n'
                'queue --monitor --root "$ROOT"\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                ["bash", str(validator), str(rendered), directory],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unresolved @PROJECT_ROOT@", result.stderr)

    def test_cron_install_renders_monitor_and_is_idempotent(self):
        script = ROOT / "hermes" / "cron-install.sh"
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory) / "hermes"
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            fake_hermes = fake_bin / "hermes"
            fake_hermes.write_text(
                """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

home = Path(os.environ["HERMES_HOME"])
jobs_file = home / "cron" / "jobs.json"
jobs_file.parent.mkdir(parents=True, exist_ok=True)
data = json.loads(jobs_file.read_text()) if jobs_file.exists() else {"jobs": []}
args = sys.argv[1:]
command = args[1] if len(args) > 1 else ""
name = args[args.index("--name") + 1] if "--name" in args else "UnicornioHater editorial pending"
jobs = data.setdefault("jobs", [])
if command == "create":
    jobs.append({"id": "fake-editorial", "name": name})
elif command == "edit":
    pass
elif command == "remove":
    jobs[:] = [job for job in jobs if job.get("id") != args[2]]
jobs_file.write_text(json.dumps(data), encoding="utf-8")
""",
                encoding="utf-8",
            )
            fake_hermes.chmod(0o755)
            env = {
                **os.environ,
                "HERMES_HOME": str(hermes_home),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            }
            first = subprocess.run(
                ["bash", str(script), "every 2h"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            second = subprocess.run(
                ["bash", str(script), "every 2h"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            monitor = hermes_home / "scripts" / "unicornio-editor-monitor.sh"
            jobs = json.loads(
                (hermes_home / "cron" / "jobs.json").read_text(encoding="utf-8")
            )["jobs"]
            monitor_exists = monitor.is_file()
            monitor_executable = os.access(monitor, os.X_OK)
            rendered = monitor.read_text(encoding="utf-8") if monitor_exists else ""
            job_count = len(jobs)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(monitor_exists)
        self.assertTrue(monitor_executable)
        self.assertNotIn("@PROJECT_ROOT@", rendered)
        self.assertIn(f'ROOT="{ROOT}"', rendered)
        self.assertEqual(job_count, 1)

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
        self.assertIn("content_not_required", content)

    def test_cost_guard_also_guards_context_volume(self):
        # Dolar nao percebe regressao de contexto: o guard tambem mede requests,
        # PROMPT tokens (input + cache_read + cache_write) e os bytes devolvidos
        # ao modelo.
        content = (ROOT / "hermes" / "cost_guard.py").read_text()
        self.assertIn("--limit-requests", content)
        self.assertIn("--limit-prompt-tokens", content)
        self.assertIn("--limit-input-tokens", content)  # alias deprecated
        self.assertIn("--limit-context-bytes", content)
        self.assertIn("prompt_tokens", content)
        monitor = (ROOT / "hermes" / "monitor.sh").read_text()
        self.assertIn("HERMES_EDITORIAL_WINDOW_CONTEXT_BYTES_LIMIT", monitor)
        self.assertIn("HERMES_EDITORIAL_WINDOW_PROMPT_TOKEN_LIMIT", monitor)

    def test_monitor_freezes_the_signature_when_the_budget_blocks(self):
        # O Hermes hasheia a saida: imprimir "BUDGET_EXHAUSTED {json}" no bloqueio
        # seria uma MUDANCA de hash e acordaria o LLM justamente no freio.
        content = (ROOT / "hermes" / "monitor.sh").read_text()
        self.assertIn("monitor_effective_output", content)
        self.assertIn("emitir_assinatura_congelada", content)
        self.assertIn("monitor-budget.log", content)
        self.assertIn("budget_epoch", content)
        self.assertIn("monitor-budget-state", content)
        self.assertNotIn("BUDGET_EXHAUSTED ", content)

    def test_env_example_documents_the_new_windows(self):
        content = (ROOT / ".env.example").read_text()
        self.assertIn("HERMES_EDITORIAL_WINDOW_PROMPT_TOKEN_LIMIT", content)
        self.assertIn("EDITOR_AUTO_SCORE_MARGIN", content)

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

    def test_monitor_resolves_stale_budget_job_id_from_hermes_catalog(self):
        template = (ROOT / "hermes" / "monitor.sh").read_text()
        actual_id = "66126ac17de0"

        def run_with_catalog(catalog, configured="fake-editorial"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "project"
                hermes_home = Path(directory) / "hermes"
                root.mkdir()
                (root / "work").mkdir()
                (root / ".env").write_text(
                    "HERMES_EDITORIAL_CRON_JOB_ID=" + configured + "\n"
                    "HERMES_EDITORIAL_WINDOW_REQUEST_LIMIT=1\n",
                    encoding="utf-8",
                )
                (hermes_home / "cron").mkdir(parents=True)
                jobs_file = hermes_home / "cron" / "jobs.json"
                if catalog is not None:
                    jobs_file.write_text(catalog, encoding="utf-8")
                venv_bin = root / ".venv" / "bin"
                venv_bin.mkdir(parents=True)
                call_log = Path(directory) / "cost-guard-args.json"
                python_wrapper = venv_bin / "python"
                python_wrapper.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, os, subprocess, sys\n"
                    "if sys.argv[1:2] == ['-']:\n"
                    f"    raise SystemExit(subprocess.call([{sys.executable!r}, *sys.argv[1:]]))\n"
                    "if sys.argv[1:2] and sys.argv[1].endswith('cost_guard.py'):\n"
                    "    with open(os.environ['CALL_LOG'], 'w', encoding='utf-8') as handle:\n"
                    "        json.dump(sys.argv[1:], handle)\n"
                    "    print(json.dumps({'decision': 'allow'}))\n"
                    "    raise SystemExit(0)\n"
                    "raise SystemExit(0)\n",
                    encoding="utf-8",
                )
                python_wrapper.chmod(0o755)
                editor = venv_bin / "unicornio-editor"
                editor.write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="utf-8")
                editor.chmod(0o755)
                monitor = root / "monitor.sh"
                monitor.write_text(
                    template.replace("@PROJECT_ROOT@", str(root)), encoding="utf-8"
                )
                monitor.chmod(0o755)
                result = subprocess.run(
                    ["bash", str(monitor)],
                    env={
                        **os.environ,
                        "HOME": directory,
                        "HERMES_HOME": str(hermes_home),
                        "CALL_LOG": str(call_log),
                    },
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads(call_log.read_text(encoding="utf-8"))
                return args[args.index("--job-id") + 1]

        one_job = json.dumps({"jobs": [{"id": actual_id, "name": "UnicornioHater editorial pending"}]})
        duplicate_jobs = json.dumps({"jobs": [
            {"id": actual_id, "name": "UnicornioHater editorial pending"},
            {"id": "other-editorial", "name": "UnicornioHater editorial pending"},
        ]})
        self.assertEqual(run_with_catalog(one_job), actual_id)
        self.assertEqual(run_with_catalog(one_job, configured=actual_id), actual_id)
        self.assertEqual(run_with_catalog(duplicate_jobs), "")
        self.assertEqual(run_with_catalog(json.dumps({"jobs": []})), "")
        self.assertEqual(run_with_catalog("not-json"), "")
        self.assertEqual(run_with_catalog(None), "")

    def test_cost_guard_is_valid_python(self):
        script = ROOT / "hermes" / "cost_guard.py"
        result = subprocess.run(["python3", "-m", "py_compile", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
