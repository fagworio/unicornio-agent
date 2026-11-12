import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SUPERVISOR_DIR = ROOT / "hermes" / "supervisor"


def _fake_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "fake-systemctl"
    log = tmp_path / "systemctl-calls.jsonl"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args=sys.argv[1:]\n"
        "with open(os.environ['SYSTEMCTL_LOG'], 'a', encoding='utf-8') as f: f.write(json.dumps(args)+'\\n')\n"
        "if args and args[0] == 'show':\n"
        "    prop=next((a.split('=',1)[1] for a in args if a.startswith('--property=')), '')\n"
        "    print(os.environ.get('FAKE_'+prop.upper(), ''))\n"
        "elif args and args[0] == 'start':\n"
        "    sys.exit(0)\n"
        "else:\n"
        "    sys.exit(3)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable, log


def _run_trigger(tmp_path: Path, *, load_state: str, active_state: str):
    executable, log = _fake_systemctl(tmp_path)
    env = {
        **os.environ,
        "SYSTEMCTL_BIN": str(executable),
        "SYSTEMCTL_LOG": str(log),
        "FAKE_LOADSTATE": load_state,
        "FAKE_ACTIVESTATE": active_state,
    }
    return subprocess.run(
        ["bash", str(SUPERVISOR_DIR / "start-v2-cycle.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    ), log


def test_trigger_starts_one_inactive_service_without_waiting(tmp_path):
    result, log = _run_trigger(tmp_path, load_state="loaded", active_state="inactive")
    calls = [json.loads(line) for line in log.read_text().splitlines()]

    assert result.returncode == 0, result.stderr
    assert "start_requested" in result.stdout
    assert ["start", "--no-block", "unicornio-editorial-v2.service"] in calls


def test_trigger_monitors_an_activating_service_without_starting_another(tmp_path):
    result, log = _run_trigger(tmp_path, load_state="loaded", active_state="activating")
    calls = [json.loads(line) for line in log.read_text().splitlines()]

    assert result.returncode == 0, result.stderr
    assert "monitor_only" in result.stdout
    assert not any(call[0] == "start" for call in calls)


def test_trigger_blocks_failed_service_without_automatic_recovery(tmp_path):
    result, log = _run_trigger(tmp_path, load_state="loaded", active_state="failed")
    calls = [json.loads(line) for line in log.read_text().splitlines()]

    assert result.returncode != 0
    assert "manual_reset_required" in result.stdout
    assert not any(call[0] in {"start", "reset-failed", "restart"} for call in calls)


def test_cycle_runner_sources_env_and_executes_only_normal_v2_cycle(tmp_path):
    root = tmp_path / "project"
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    evidence = tmp_path / "runner.json"
    root.joinpath(".env").write_text("SUPERVISOR_TEST_VALUE=loaded\n", encoding="utf-8")
    python.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "json.dump({'argv':sys.argv[1:], 'cwd':os.getcwd(), 'env':os.environ.get('SUPERVISOR_TEST_VALUE'), 'pythonpath':os.environ.get('PYTHONPATH')}, open(os.environ['RUNNER_EVIDENCE'],'w'))\n",
        encoding="utf-8",
    )
    python.chmod(0o700)
    env = {
        **os.environ,
        "UNICORNIO_EDITORIAL_ROOT": str(root),
        "RUNNER_EVIDENCE": str(evidence),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / ".hermes"),
    }
    result = subprocess.run(
        ["bash", str(SUPERVISOR_DIR / "run-v2-cycle.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(evidence.read_text())

    assert result.returncode == 0, result.stderr
    assert payload["argv"] == ["-m", "unicornio_editor.cli", "v2-cycle", "--root", str(root)]
    assert payload["cwd"] == str(root)
    assert payload["env"] == "loaded"
    assert payload["pythonpath"] == f"{root}:{root / 'src'}"


def test_systemd_unit_is_single_shot_and_has_no_automatic_restart():
    unit = (ROOT / "systemd" / "unicornio-editorial-v2.service").read_text()

    assert "Type=oneshot" in unit
    assert "Restart=no" in unit
    assert "TimeoutStartSec=0" in unit
    assert "TimeoutStopSec=0" in unit
    assert "Restart=always" not in unit
    assert "Restart=on-failure" not in unit
    assert "[Install]" not in unit
