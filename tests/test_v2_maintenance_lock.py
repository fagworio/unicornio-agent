from pathlib import Path

from unicornio_editor.pipeline_v2.lock import RunSessionLock


def test_run_lock_refuses_maintenance_gate(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "v2-maintenance.lock").write_text("maintenance")
    lock = RunSessionLock(work / "v2-run.lock")
    assert lock.acquire() is False
    bypass = RunSessionLock(work / "v2-run-reset.lock", allow_maintenance=True)
    assert bypass.acquire() is True
    bypass.release()
