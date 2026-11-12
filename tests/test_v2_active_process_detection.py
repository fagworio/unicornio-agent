import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_process_scan_does_not_count_its_own_matching_argv(tmp_path):
    probe = tmp_path / "scan_probe.py"
    probe.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from unicornio_editor.pipeline_v2.reset_epoch import _active_processes\n"
        "print(json.dumps({'matches': _active_processes()}))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(probe), "v2-cycle"],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(result.stdout)

    assert result.returncode == 0, result.stderr
    assert not any(str(probe) in command for command in payload["matches"])


def test_process_scan_still_detects_a_separate_v2_worker():
    from unicornio_editor.pipeline_v2.reset_epoch import _active_processes

    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)", "unicornio_editor.cli", "v2-cycle"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        matches = _active_processes()
        assert any(command.startswith(f"{worker.pid}: ") for command in matches)
    finally:
        worker.terminate()
        worker.wait(timeout=5)
