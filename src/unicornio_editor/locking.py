"""Cross-process per-post locks with legacy JSON lock compatibility."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any


class LockError(RuntimeError):
    """Raised when a post lock cannot be acquired or ownership is lost."""


class LockHandle:
    def __init__(self, path: Path, fd: int, token: str, ttl: int):
        self.path = path
        self._fd = fd
        self.token = token
        self.ttl = ttl
        self._released = False
        self._lost = False
        self._stop = threading.Event()
        self._io_lock = threading.Lock()
        self._heartbeat: threading.Thread | None = None

    def _read_record(self) -> dict[str, Any] | None:
        os.lseek(self._fd, 0, os.SEEK_SET)
        raw = os.read(self._fd, 8192)
        if not raw:
            return None
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None

    def _write_record(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.ftruncate(self._fd, 0)
        view = memoryview(payload)
        while view:
            written = os.write(self._fd, view)
            if written <= 0:
                raise OSError("short write while updating post lock")
            view = view[written:]
        os.fsync(self._fd)

    def _assert_owned_unlocked(self) -> dict[str, Any]:
        if self._released or self._lost or self._stop.is_set():
            raise LockError("post lock is no longer owned by this handle")
        try:
            path_stat = self.path.stat()
            fd_stat = os.fstat(self._fd)
            if (path_stat.st_dev, path_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
                raise LockError("post lock file was replaced while held")
            data = self._read_record()
        except (FileNotFoundError, OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._lost = True
            self._stop.set()
            raise LockError("post lock ownership could not be verified") from exc
        if not data or data.get("token") != self.token or data.get("released"):
            self._lost = True
            self._stop.set()
            raise LockError("post lock ownership token changed")
        return data

    def assert_owned(self) -> None:
        """Fail before a side effect if the lock path/token no longer belongs to this handle."""
        with self._io_lock:
            self._assert_owned_unlocked()

    def refresh(self) -> bool:
        """Renew only while this process still owns the locked inode."""
        with self._io_lock:
            try:
                data = self._assert_owned_unlocked()
                data["created_at"] = time.time()
                self._write_record(data)
                return True
            except LockError:
                return False
            except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
                self._lost = True
                self._stop.set()
                return False

    def release(self) -> None:
        """Mark the stable lock file released, then drop the kernel lock."""
        with self._io_lock:
            if self._released:
                return
            try:
                data = self._assert_owned_unlocked()
                data["released"] = True
                data["released_at"] = time.time()
                self._write_record(data)
            except (LockError, OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
                # The kernel lock remains authoritative; do not unlink a file
                # whose contents may belong to a newer or legacy owner.
                pass
            self._released = True
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)

    def __enter__(self) -> "LockHandle":
        interval = max(1.0, self.ttl / 3)

        def heartbeat() -> None:
            while not self._stop.wait(interval):
                if not self.refresh():
                    return

        self._heartbeat = threading.Thread(target=heartbeat, daemon=True)
        self._heartbeat.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=1)
        self.release()


class LockManager:
    def __init__(self, root: Path, *, ttl: int = 900):
        if ttl < 1:
            raise ValueError("ttl must be positive")
        self.root = Path(root)
        self.ttl = ttl
        self.root.mkdir(parents=True, exist_ok=True)

    def acquire(self, post_id: int) -> LockHandle:
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id < 1:
            raise LockError("post id must be a positive integer")
        path = self.root / f"{post_id}.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, os.O_RDWR)
            created = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockError(f"post {post_id} is already locked") from exc

            prior = self._read_legacy_record(fd)
            has_kernel_owner_record = bool(
                prior and prior.get("token") and isinstance(prior.get("pid"), int)
            )
            if prior is not None and not prior.get("released") and not has_kernel_owner_record:
                try:
                    created_at = float(prior["created_at"])
                    fresh = time.time() - created_at <= self.ttl
                except (KeyError, TypeError, ValueError):
                    fresh = self._file_is_recent(path, self.ttl)
                if fresh:
                    raise LockError(f"post {post_id} is held by a legacy lock owner")
            elif prior is None and not created and self._file_is_recent(path, self.ttl):
                # An empty/replaced lock file may be between open and JSON
                # initialization in a legacy process. Ambiguity is a lock.
                raise LockError(f"post {post_id} has an uninitialized legacy lock")

            token = secrets.token_urlsafe(24)
            handle = LockHandle(path, fd, token, self.ttl)
            handle._write_record({
                "token": token,
                "created_at": time.time(),
                "post_id": post_id,
                "pid": os.getpid(),
                "released": False,
            })
            return handle
        except Exception:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            raise

    @staticmethod
    def _read_legacy_record(fd: int) -> dict[str, Any] | None:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 8192)
        if not raw:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _file_is_recent(path: Path, ttl: int) -> bool:
        try:
            return time.time() - path.stat().st_mtime <= ttl
        except FileNotFoundError:
            return False
