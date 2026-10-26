"""Exclusive process-wide V2 session lock."""

import fcntl
from pathlib import Path


class RunSessionLock:
    def __init__(self, path: str | Path, *, allow_maintenance: bool = False):
        self.path = Path(path)
        self.allow_maintenance = allow_maintenance
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        maintenance = self.path.parent / "v2-maintenance.lock"
        if maintenance.exists() and not self.allow_maintenance:
            return False
        self._handle = self.path.open("a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self._handle.close()
            self._handle = None
            return False

    def release(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("run-session is already locked")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
