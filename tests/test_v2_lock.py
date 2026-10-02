import tempfile
from pathlib import Path

from unicornio_editor.pipeline_v2.lock import RunSessionLock


def test_run_session_lock_is_exclusive():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "run-session.lock"
        first = RunSessionLock(path)
        second = RunSessionLock(path)
        assert first.acquire() is True
        assert second.acquire() is False
        first.release()
        assert second.acquire() is True
        second.release()
