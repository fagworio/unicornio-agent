"""Exclusive process-wide V2 session lock."""

import fcntl
from pathlib import Path


class RunSessionLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
