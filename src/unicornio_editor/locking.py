"""Cross-process per-post locks with legacy JSON lock compatibility."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import socket
import stat
import threading
import time
import uuid
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
        # ``_stop`` controls heartbeat scheduling; it is not evidence that
        # ownership was lost.  In particular, context-manager exit stops the
        # heartbeat before writing the final released record.
        if self._released or self._lost:
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
            if self._stop.is_set():
                return False
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
        """Persist release while ownership is valid, then drop the kernel lock.

        A failed ownership check never rewrites another owner's record, but
        the descriptor is always unlocked and closed.  Persistence failures
        are surfaced to the caller instead of being silently discarded.
        """
        self._stop.set()
        heartbeat = self._heartbeat
        if heartbeat is not None and heartbeat is not threading.current_thread():
            heartbeat.join()

        release_error: Exception | None = None
        with self._io_lock:
            if self._released:
                return
            try:
                data = self._assert_owned_unlocked()
                data["released"] = True
                data["released_at"] = time.time()
                self._write_record(data)
            except (LockError, OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
                # Do not alter a file whose identity/token may belong to a
                # newer owner.  Still release our kernel descriptor below.
                release_error = exc
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError as exc:
                release_error = release_error or exc
            try:
                os.close(self._fd)
            except OSError as exc:
                release_error = release_error or exc
            finally:
                self._released = True
        if release_error is not None:
            if isinstance(release_error, LockError):
                raise release_error
            raise LockError("post lock release or release-record persistence failed") from release_error

    def __enter__(self) -> "LockHandle":
        interval = max(1.0, self.ttl / 3)

        def heartbeat() -> None:
            while not self._stop.wait(interval):
                if not self.refresh():
                    return

        self._heartbeat = threading.Thread(target=heartbeat, daemon=True)
        self._heartbeat.start()
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, _tb: object) -> None:
        try:
            self.release()
        except LockError as release_error:
            if exc is None:
                raise
            # Preserve the work exception while making lock cleanup failure
            # visible in traceback diagnostics.
            if hasattr(exc, "add_note"):
                exc.add_note(f"post lock cleanup also failed: {release_error}")


def _process_start_identity(pid: int) -> str | None:
    """Return Linux /proc start ticks, fencing PID reuse where available."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = raw[raw.rfind(")") + 2 :].split()
        return tail[19]  # field 22 (starttime); tail begins at field 3
    except (OSError, IndexError):
        return None


def _read_lock_snapshot(path: Path, *, exclusive: bool) -> tuple[int, dict[str, Any] | None, str | None]:
    flags = os.O_RDWR if exclusive else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return fd, None, "non_regular_lock_file"
        current = path.stat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            return fd, None, "lock_file_replaced_during_check"
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            return fd, None, "kernel_lock_busy"
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 8192)
        if not raw:
            return fd, None, "empty_lock_record"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return fd, None, "invalid_lock_record"
        if not isinstance(value, dict):
            return fd, None, "invalid_lock_record"
        return fd, value, None
    except Exception:
        os.close(fd)
        raise


def _owner_record_sha256(record: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _lock_path_matches_fd(path: Path, fd: int) -> bool:
    try:
        path_stat = path.stat()
        fd_stat = os.fstat(fd)
    except OSError:
        return False
    return (path_stat.st_dev, path_stat.st_ino) == (fd_stat.st_dev, fd_stat.st_ino)


def _owner_process_is_gone(record: dict[str, Any]) -> tuple[bool, str]:
    host = str(record.get("host") or "")
    if not host or host != socket.gethostname():
        return False, "owner_host_missing_or_different"
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return False, "owner_pid_missing_or_invalid"
    expected_start = str(record.get("process_start") or "")
    if not expected_start:
        return False, "owner_process_identity_missing"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True, "owner_process_absent"
    except PermissionError:
        return False, "owner_process_status_unavailable"
    except OSError:
        return False, "owner_process_status_unavailable"
    actual_start = _process_start_identity(pid)
    if actual_start is None:
        return False, "owner_process_identity_unavailable"
    if actual_start != expected_start:
        return True, "pid_reused_after_owner_exit"
    return False, "owner_process_still_active"


def _secure_atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Write a private audit record with atomic replacement and mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def reconcile_stale_post_lock(
    root: Path,
    post_id: int,
    *,
    apply: bool = False,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Preview or auditably release a dead owner's free per-post lock.

    Ambiguous, legacy, remote-host, live-process, replaced, or kernel-busy
    locks remain untouched.  Applying requires the exact preview hash.
    """
    if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id < 1:
        return {"status": "blocked", "reason": "invalid_post_id", "lock_writes": 0}
    work_root = Path(root) / "work"
    lock_root = work_root / "locks"
    audit_root = work_root / "lock-reconciliations"
    if work_root.is_symlink() or lock_root.is_symlink() or audit_root.is_symlink():
        return {"status": "blocked", "reason": "lock_directory_is_symlink", "lock_writes": 0}
    path = lock_root / f"{post_id}.lock"
    if path.is_symlink():
        return {"status": "blocked", "reason": "lock_file_is_symlink", "lock_writes": 0}
    if not path.is_file():
        return {"status": "no_lock_record", "read_only": not apply, "lock_writes": 0}

    fd = -1
    try:
        fd, record, error = _read_lock_snapshot(path, exclusive=apply)
        if error == "kernel_lock_busy":
            return {"status": "blocked", "reason": error, "lock_writes": 0}
        if error:
            return {"status": "blocked", "reason": error, "lock_writes": 0}
        assert record is not None
        if not _lock_path_matches_fd(path, fd):
            return {"status": "blocked", "reason": "lock_file_replaced_during_check", "lock_writes": 0}
        if record.get("released") is True:
            reconciliation_id = str(record.get("reconciliation_id") or "")
            if reconciliation_id:
                archive = audit_root / f"{post_id}-{reconciliation_id}.json"
                if archive.is_symlink():
                    return {"status": "blocked", "reason": "reconciliation_archive_is_symlink", "lock_writes": 0}
                if archive.is_file():
                    audit = json.loads(archive.read_text(encoding="utf-8"))
                    if audit.get("status") == "prepared" and apply:
                        audit["status"] = "completed"
                        audit["completed_at"] = time.time()
                        _secure_atomic_json(archive, audit)
                    return {
                        "status": "already_reconciled",
                        "reconciliation_id": reconciliation_id,
                        "lock_writes": 0,
                        "read_only": not apply,
                    }
            return {"status": "already_released", "lock_writes": 0, "read_only": not apply}
        if not record.get("token"):
            return {"status": "blocked", "reason": "owner_token_missing", "lock_writes": 0}
        if record.get("released") is not False:
            return {"status": "blocked", "reason": "owner_state_ambiguous", "lock_writes": 0}
        dead, reason = _owner_process_is_gone(record)
        if not dead:
            return {"status": "blocked", "reason": reason, "lock_writes": 0}
        snapshot_hash = _owner_record_sha256(record)
        if not apply:
            return {
                "status": "reconciliation_available",
                "read_only": True,
                "post_id": post_id,
                "owner_record_sha256": snapshot_hash,
                "owner_pid": int(record["pid"]),
                "owner_host": str(record["host"]),
                "owner_process_status": reason,
                "lock_writes": 0,
            }
        if expected_sha256 != snapshot_hash:
            return {
                "status": "blocked",
                "reason": "preview_snapshot_mismatch",
                "current_owner_record_sha256": snapshot_hash,
                "lock_writes": 0,
            }
        reconciliation_id = hashlib.sha256(
            f"{post_id}:{snapshot_hash}".encode("utf-8")
        ).hexdigest()[:32]
        archive = audit_root / f"{post_id}-{reconciliation_id}.json"
        if archive.is_symlink():
            return {"status": "blocked", "reason": "reconciliation_archive_is_symlink", "lock_writes": 0}
        redacted = {key: value for key, value in record.items() if key != "token"}
        redacted["token_sha256"] = hashlib.sha256(str(record["token"]).encode()).hexdigest()
        audit = {
            "schema_version": 1,
            "status": "prepared",
            "reconciliation_id": reconciliation_id,
            "post_id": post_id,
            "created_at": time.time(),
            "reason": reason,
            "owner_record_sha256": snapshot_hash,
            "previous_owner_record": redacted,
        }
        if archive.exists():
            existing = json.loads(archive.read_text(encoding="utf-8"))
            if existing.get("owner_record_sha256") != snapshot_hash:
                return {"status": "blocked", "reason": "reconciliation_archive_conflict", "lock_writes": 0}
        else:
            _secure_atomic_json(archive, audit)
        if not _lock_path_matches_fd(path, fd):
            return {"status": "blocked", "reason": "lock_file_replaced_before_update", "lock_writes": 0}
        record["released"] = True
        record["released_at"] = time.time()
        record["reconciliation_id"] = reconciliation_id
        record["reconciliation_reason"] = reason
        payload = json.dumps(record, separators=(",", ":")).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short write while reconciling lock record")
            view = view[count:]
        os.fsync(fd)
        if not _lock_path_matches_fd(path, fd):
            return {"status": "blocked", "reason": "lock_file_replaced_after_update", "lock_writes": 0}
        audit["status"] = "completed"
        audit["completed_at"] = time.time()
        _secure_atomic_json(archive, audit)
        return {
            "status": "reconciled",
            "post_id": post_id,
            "reconciliation_id": reconciliation_id,
            "owner_record_sha256": snapshot_hash,
            "lock_writes": 1,
            "audit_path": str(archive),
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "blocked", "reason": "reconciliation_io_error", "error_class": type(exc).__name__, "lock_writes": 0}
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


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
                "host": socket.gethostname(),
                "process_start": _process_start_identity(os.getpid()),
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
