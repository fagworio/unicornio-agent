"""Cross-process per-post locks with legacy JSON lock compatibility."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
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
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_nonstandard_json,
        )
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
        locked_info = os.fstat(fd)
        locked_path = path.stat()
        if (locked_info.st_dev, locked_info.st_ino) != (locked_path.st_dev, locked_path.st_ino):
            return fd, None, "lock_file_replaced_during_check"
        if locked_info.st_size > 8192:
            return fd, None, "lock_record_size_limit_exceeded"
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 8192)
        after_read = os.fstat(fd)
        after_path = path.stat()
        if (
            (locked_info.st_dev, locked_info.st_ino, locked_info.st_size, locked_info.st_mtime_ns)
            != (after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns)
            or (after_read.st_dev, after_read.st_ino) != (after_path.st_dev, after_path.st_ino)
            or len(raw) != after_read.st_size
        ):
            return fd, None, "lock_record_changed_during_read"
        if not raw:
            return fd, None, "empty_lock_record"
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_nonstandard_json,
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
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


def _lock_snapshot_identity(path: Path, fd: int) -> dict[str, Any] | None:
    """Describe the exact lock-file instance and bytes while its flock is held."""
    try:
        descriptor_stat = os.fstat(fd)
        path_stat = path.stat()
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or (descriptor_stat.st_dev, descriptor_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
            or descriptor_stat.st_size > 8192
        ):
            return None
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 8193)
        if len(raw) > 8192:
            return None
        after_stat = os.fstat(fd)
        after_path_stat = path.stat()
        if (
            (descriptor_stat.st_dev, descriptor_stat.st_ino, descriptor_stat.st_mtime_ns)
            != (after_stat.st_dev, after_stat.st_ino, after_stat.st_mtime_ns)
            or (after_stat.st_dev, after_stat.st_ino)
            != (after_path_stat.st_dev, after_path_stat.st_ino)
            or len(raw) != after_stat.st_size
        ):
            return None
        return {
            "owner_device": int(after_stat.st_dev),
            "owner_inode": int(after_stat.st_ino),
            "owner_mtime_ns": int(after_stat.st_mtime_ns),
            "owner_record_bytes_sha256": hashlib.sha256(raw).hexdigest(),
            "owner_record_size_bytes": len(raw),
        }
    except OSError:
        return None


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


def classify_lock_owner_record(
    record: dict[str, Any] | None, *, host: str | None = None
) -> tuple[str, str]:
    """Return one shared conservative classification for lock owner records."""
    if not isinstance(record, dict):
        return "corrupt", "invalid_lock_record"
    if record.get("released") is True:
        token = record.get("token")
        if not isinstance(token, str) or not token.strip():
            return "corrupt", "released_owner_token_missing"
        return "released", "owner_record_released"
    if record.get("released") is not False:
        return "corrupt", "owner_state_ambiguous"
    token = record.get("token")
    if not isinstance(token, str) or not token.strip():
        return "legacy_unverifiable", "owner_token_missing"
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return "legacy_unverifiable", "owner_pid_missing_or_invalid"
    owner_host = str(record.get("host") or "")
    if not owner_host or owner_host != (host or socket.gethostname()):
        return "legacy_unverifiable", "owner_host_missing_or_different"
    if not str(record.get("process_start") or ""):
        return "legacy_unverifiable", "owner_process_identity_missing"
    dead, reason = _owner_process_is_gone(record)
    if dead:
        return "dead_proven", reason
    if reason == "owner_process_still_active":
        return "live", reason
    return "legacy_unverifiable", reason


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


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_nonstandard_json(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")


def _read_private_audit(path: Path) -> dict[str, Any]:
    """Read a bounded, private audit object without following a replaced path."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("audit_path_not_regular")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("audit_permissions_too_open")
    if info.st_size > 1024 * 1024:
        raise ValueError("audit_size_limit_exceeded")
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(fd)
        current = path.stat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or stat.S_IMODE(opened.st_mode) & 0o077
            or opened.st_size > 1024 * 1024
        ):
            raise ValueError("audit_replaced_or_invalid")
        chunks: list[bytes] = []
        remaining = 1024 * 1024 + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > 1024 * 1024:
            raise ValueError("audit_size_limit_exceeded")
        final = os.fstat(fd)
        final_path = path.stat()
        if (
            (opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_size)
            != (final.st_dev, final.st_ino, final.st_mtime_ns, final.st_size)
            or (final.st_dev, final.st_ino) != (final_path.st_dev, final_path.st_ino)
            or len(raw) != final.st_size
        ):
            raise ValueError("audit_changed_during_read")
    finally:
        os.close(fd)
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_nonstandard_json,
        )
    if not isinstance(value, dict):
        raise ValueError("audit_not_object")
    return value


def _validate_reconciliation_audit(
    audit: dict[str, Any],
    current_record: dict[str, Any],
    *,
    post_id: int,
    reconciliation_id: str,
    legacy: bool,
) -> bool:
    """Verify released lock, redacted prior owner, and audit identity agree."""
    if (
        current_record.get("released") is not True
        or current_record.get("reconciliation_id") != reconciliation_id
        or audit.get("schema_version") != 1
        or audit.get("status") not in {"prepared", "completed"}
        or audit.get("post_id") != post_id
        or audit.get("reconciliation_id") != reconciliation_id
    ):
        return False
    common_keys = {
        "schema_version", "status", "reconciliation_id", "post_id", "created_at",
        "owner_record_sha256", "owner_device", "owner_inode", "owner_mtime_ns",
        "owner_record_bytes_sha256", "owner_record_size_bytes", "previous_owner_record",
    }
    required_keys = common_keys
    allowed_keys = common_keys | {"reason"}
    if legacy:
        allowed_keys |= {"external_proof", "authorization_id", "authorized_by"}
        required_keys |= {"external_proof", "authorization_id", "authorized_by"}
    else:
        required_keys |= {"reason"}
    if not required_keys.issubset(audit) or set(audit) - (allowed_keys | {"completed_at"}):
        return False
    if (
        isinstance(audit.get("owner_device"), bool)
        or not isinstance(audit.get("owner_device"), int)
        or isinstance(audit.get("owner_inode"), bool)
        or not isinstance(audit.get("owner_inode"), int)
        or isinstance(audit.get("owner_mtime_ns"), bool)
        or not isinstance(audit.get("owner_mtime_ns"), int)
        or isinstance(audit.get("owner_record_size_bytes"), bool)
        or not isinstance(audit.get("owner_record_size_bytes"), int)
        or audit["owner_record_size_bytes"] < 1
        or not re.fullmatch(r"[0-9a-f]{64}", str(audit.get("owner_record_bytes_sha256") or ""))
    ):
        return False
    previous = audit.get("previous_owner_record")
    token = current_record.get("token")
    if not isinstance(previous, dict) or "token" in previous or not isinstance(token, str) or not token:
        return False
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if previous.get("token_sha256") != token_hash:
        return False
    reconstructed = dict(previous)
    reconstructed.pop("token_sha256", None)
    reconstructed["token"] = token
    owner_hash = audit.get("owner_record_sha256")
    if (
        not isinstance(owner_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", owner_hash)
        or _owner_record_sha256(reconstructed) != owner_hash
        or reconstructed.get("released") is not False
    ):
        return False
    if current_record.get("reconciliation_reason") != audit.get("reason") and not legacy:
        return False
    if legacy:
        proof = audit.get("external_proof")
        if (
            not isinstance(proof, dict)
            or set(proof) != {"owner_source_verified", "writer_processes_stopped", "proof_reference_sha256"}
            or proof.get("owner_source_verified") is not True
            or proof.get("writer_processes_stopped") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", str(proof.get("proof_reference_sha256") or ""))
            or not str(audit.get("authorization_id") or "").strip()
            or not str(audit.get("authorized_by") or "").strip()
            or current_record.get("reconciliation_reason") != "externally_verified_legacy_owner_recovery"
        ):
            return False
    return True


def reconcile_stale_post_lock(
    root: Path,
    post_id: int,
    *,
    apply: bool = False,
    expected_sha256: str | None = None,
    expected_device: int | None = None,
    expected_inode: int | None = None,
    expected_mtime_ns: int | None = None,
    expected_bytes_sha256: str | None = None,
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
    lock_writes = 0
    try:
        fd, record, error = _read_lock_snapshot(path, exclusive=apply)
        if error == "kernel_lock_busy":
            return {"status": "blocked", "reason": error, "lock_writes": 0}
        if error:
            return {"status": "blocked", "reason": error, "lock_writes": 0}
        assert record is not None
        snapshot = _lock_snapshot_identity(path, fd)
        if snapshot is None:
            return {"status": "blocked", "reason": "lock_snapshot_changed_or_invalid", "lock_writes": 0}
        if not _lock_path_matches_fd(path, fd):
            return {"status": "blocked", "reason": "lock_file_replaced_during_check", "lock_writes": 0}
        if record.get("released") is True:
            reconciliation_id = str(record.get("reconciliation_id") or "")
            if reconciliation_id:
                archive = audit_root / f"{post_id}-{reconciliation_id}.json"
                if archive.is_symlink():
                    return {"status": "blocked", "reason": "reconciliation_archive_is_symlink", "lock_writes": 0}
                if not archive.is_file():
                    return {"status": "blocked", "reason": "reconciliation_audit_missing", "lock_writes": 0}
                audit = _read_private_audit(archive)
                if not _validate_reconciliation_audit(
                    audit, record, post_id=post_id, reconciliation_id=reconciliation_id, legacy=False
                ):
                    return {"status": "blocked", "reason": "reconciliation_audit_mismatch", "lock_writes": 0}
                if audit.get("status") == "prepared":
                    if not apply:
                        return {
                            "status": "reconciliation_audit_pending",
                            "reconciliation_id": reconciliation_id,
                            "lock_writes": 0,
                            "read_only": True,
                        }
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
        owner_status, reason = classify_lock_owner_record(record)
        if owner_status != "dead_proven":
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
                **snapshot,
                "lock_writes": 0,
            }
        if expected_sha256 != snapshot_hash:
            return {
                "status": "blocked",
                "reason": "preview_snapshot_mismatch",
                "current_owner_record_sha256": snapshot_hash,
                "lock_writes": 0,
            }
        expected_snapshot = {
            "owner_device": expected_device,
            "owner_inode": expected_inode,
            "owner_mtime_ns": expected_mtime_ns,
            "owner_record_bytes_sha256": expected_bytes_sha256,
        }
        if any(value is None for value in expected_snapshot.values()):
            return {
                "status": "blocked",
                "reason": "preview_snapshot_metadata_required",
                "lock_writes": 0,
            }
        if any(snapshot[key] != value for key, value in expected_snapshot.items()):
            return {
                "status": "blocked",
                "reason": "preview_snapshot_metadata_mismatch",
                "current_snapshot": snapshot,
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
            **snapshot,
            "previous_owner_record": redacted,
        }
        if archive.exists():
            existing = _read_private_audit(archive)
            if (
                existing.get("status") != "prepared"
                or set(existing) != set(audit)
                or any(existing.get(key) != value for key, value in audit.items() if key != "created_at")
            ):
                return {"status": "blocked", "reason": "reconciliation_archive_conflict", "lock_writes": 0}
            audit = existing
        else:
            _secure_atomic_json(archive, audit)
        if (
            not _lock_path_matches_fd(path, fd)
            or _lock_snapshot_identity(path, fd) != snapshot
        ):
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
        lock_writes = 1
        readback = json.loads(os.pread(fd, 8192, 0).decode("utf-8"))
        if (
            not _lock_path_matches_fd(path, fd)
            or readback.get("released") is not True
            or readback.get("reconciliation_id") != reconciliation_id
            or readback.get("token") != record.get("token")
        ):
            return {"status": "blocked", "reason": "lock_readback_mismatch", "lock_writes": lock_writes}
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
        return {"status": "blocked", "reason": "reconciliation_io_error", "error_class": type(exc).__name__, "lock_writes": lock_writes}
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


def reconcile_legacy_post_lock(
    root: Path,
    post_id: int,
    *,
    apply: bool = False,
    preview: dict[str, Any] | None = None,
    external_proof: dict[str, Any] | None = None,
    authorized_by: str | None = None,
    authorization_id: str | None = None,
) -> dict[str, Any]:
    """Propose or explicitly reconcile a legacy owner with missing identity.

    Apply is gated by an externally prepared proof bundle, a human operator
    authorization, a second exclusive-lock read, and exact byte/inode/mtime
    compare-and-swap against the read-only preview. This function never
    examines or changes cycle, WordPress, quarantine, or Registry state.
    """
    if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id < 1:
        return {"status": "blocked", "reason": "invalid_post_id", "lock_writes": 0}
    work_root = Path(root) / "work"
    lock_root = work_root / "locks"
    audit_root = work_root / "lock-reconciliations"
    if any(path.is_symlink() for path in (work_root, lock_root, audit_root)):
        return {"status": "blocked", "reason": "lock_directory_is_symlink", "lock_writes": 0}
    path = lock_root / f"{post_id}.lock"
    if path.is_symlink():
        return {"status": "blocked", "reason": "lock_file_is_symlink", "lock_writes": 0}
    if not path.is_file():
        return {"status": "no_lock_record", "read_only": not apply, "lock_writes": 0}

    fd = -1
    lock_writes = 0
    try:
        fd, record, error = _read_lock_snapshot(path, exclusive=apply)
        if error:
            return {"status": "blocked", "reason": error, "lock_writes": 0}
        if not isinstance(record, dict):
            return {"status": "blocked", "reason": "corrupt_lock_record", "lock_writes": 0}
        snapshot = _lock_snapshot_identity(path, fd)
        if snapshot is None:
            return {"status": "blocked", "reason": "lock_snapshot_changed_or_invalid", "lock_writes": 0}
        owner_status, owner_reason = classify_lock_owner_record(record)
        if record.get("released") is True:
            reconciliation_id = str(record.get("reconciliation_id") or "")
            if reconciliation_id:
                archive = audit_root / f"{post_id}-legacy-{reconciliation_id}.json"
                if archive.is_symlink():
                    return {"status": "blocked", "reason": "reconciliation_archive_is_symlink", "lock_writes": 0}
                if not archive.is_file():
                    return {"status": "blocked", "reason": "reconciliation_audit_missing", "lock_writes": 0}
                audit = _read_private_audit(archive)
                if not _validate_reconciliation_audit(
                    audit, record, post_id=post_id, reconciliation_id=reconciliation_id, legacy=True
                ):
                    return {"status": "blocked", "reason": "reconciliation_audit_mismatch", "lock_writes": 0}
                if audit.get("status") == "prepared":
                    if not apply:
                        return {
                            "status": "reconciliation_audit_pending",
                            "post_id": post_id,
                            "reconciliation_id": reconciliation_id,
                            "lock_writes": 0,
                            "read_only": True,
                        }
                    audit["status"] = "completed"
                    audit["completed_at"] = time.time()
                    _secure_atomic_json(archive, audit)
                return {
                    "status": "already_reconciled",
                    "post_id": post_id,
                    "reconciliation_id": reconciliation_id,
                    "lock_writes": 0,
                    "read_only": not apply,
                }
            return {
                "status": "already_released",
                "post_id": post_id,
                "reconciliation_id": record.get("reconciliation_id"),
                "lock_writes": 0,
                "read_only": not apply,
            }
        if owner_status != "legacy_unverifiable":
            return {
                "status": "blocked",
                "reason": "owner_is_not_legacy_unverifiable",
                "owner_status": owner_status,
                "owner_reason": owner_reason,
                "lock_writes": 0,
            }
        if not isinstance(record.get("token"), str) or not record["token"].strip():
            return {"status": "blocked", "reason": "legacy_owner_token_missing", "lock_writes": 0}
        if isinstance(record.get("pid"), bool) or not isinstance(record.get("pid"), int) or record["pid"] < 1:
            return {"status": "blocked", "reason": "legacy_owner_pid_missing", "lock_writes": 0}

        owner_hash = _owner_record_sha256(record)
        evidence = {
            "status": "legacy_recovery_proposed",
            "reason": "external_owner_and_writer_quiescence_proof_required",
            "read_only": True,
            "post_id": post_id,
            "owner_status": owner_status,
            "owner_reason": owner_reason,
            "owner_record_sha256": owner_hash,
            **snapshot,
            "lock_writes": 0,
            "automatic_recovery": False,
        }
        if not apply:
            return evidence

        if not isinstance(preview, dict) or not isinstance(external_proof, dict):
            return {"status": "blocked", "reason": "preview_and_external_proof_required", "lock_writes": 0}
        operator = str(authorized_by or "").strip()
        auth_id = str(authorization_id or "").strip()
        if not operator or len(operator) > 120 or not auth_id or len(auth_id) > 160:
            return {"status": "blocked", "reason": "human_authorization_required", "lock_writes": 0}
        if preview.get("status") != "legacy_recovery_proposed" or any(
            preview.get(key) != evidence.get(key)
            for key in (
                "post_id", "owner_record_sha256", "owner_device", "owner_inode",
                "owner_mtime_ns", "owner_record_bytes_sha256", "owner_record_size_bytes",
            )
        ):
            return {"status": "blocked", "reason": "preview_snapshot_mismatch", "lock_writes": 0}
        required_proof = {
            "post_id": post_id,
            "owner_record_sha256": owner_hash,
            "owner_record_bytes_sha256": snapshot["owner_record_bytes_sha256"],
            "owner_device": snapshot["owner_device"],
            "owner_inode": snapshot["owner_inode"],
            "owner_mtime_ns": snapshot["owner_mtime_ns"],
        }
        if any(external_proof.get(key) != value for key, value in required_proof.items()):
            return {"status": "blocked", "reason": "external_proof_snapshot_mismatch", "lock_writes": 0}
        proof_reference = str(external_proof.get("proof_reference_sha256") or "")
        if (
            external_proof.get("owner_source_verified") is not True
            or external_proof.get("writer_processes_stopped") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", proof_reference)
        ):
            return {"status": "blocked", "reason": "external_proof_incomplete", "lock_writes": 0}

        current_snapshot = _lock_snapshot_identity(path, fd)
        if current_snapshot != snapshot or not _lock_path_matches_fd(path, fd):
            return {"status": "blocked", "reason": "lock_changed_before_update", "lock_writes": 0}
        reconciliation_id = hashlib.sha256(
            f"legacy:{post_id}:{snapshot['owner_record_bytes_sha256']}:{auth_id}:{proof_reference}".encode()
        ).hexdigest()[:32]
        if audit_root.is_symlink():
            return {"status": "blocked", "reason": "lock_directory_is_symlink", "lock_writes": 0}
        archive = audit_root / f"{post_id}-legacy-{reconciliation_id}.json"
        if archive.is_symlink():
            return {"status": "blocked", "reason": "reconciliation_archive_is_symlink", "lock_writes": 0}
        redacted = {key: value for key, value in record.items() if key != "token"}
        redacted["token_sha256"] = hashlib.sha256(record["token"].encode()).hexdigest()
        audit = {
            "schema_version": 1,
            "status": "prepared",
            "reconciliation_id": reconciliation_id,
            "post_id": post_id,
            "created_at": time.time(),
            "owner_record_sha256": owner_hash,
            **snapshot,
            "external_proof": {
                "owner_source_verified": True,
                "writer_processes_stopped": True,
                "proof_reference_sha256": proof_reference,
            },
            "authorization_id": auth_id,
            "authorized_by": operator,
            "previous_owner_record": redacted,
        }
        if archive.exists():
            existing = _read_private_audit(archive)
            if (
                existing.get("status") != "prepared"
                or set(existing) != set(audit)
                or any(existing.get(key) != value for key, value in audit.items() if key != "created_at")
            ):
                return {"status": "blocked", "reason": "reconciliation_archive_conflict", "lock_writes": 0}
            audit = existing
        else:
            _secure_atomic_json(archive, audit)

        record["released"] = True
        record["released_at"] = time.time()
        record["reconciliation_id"] = reconciliation_id
        record["reconciliation_reason"] = "externally_verified_legacy_owner_recovery"
        payload = json.dumps(record, separators=(",", ":")).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while reconciling legacy lock")
            view = view[written:]
        os.fsync(fd)
        lock_writes = 1
        readback = json.loads(os.pread(fd, 8192, 0).decode("utf-8"))
        if (
            not _lock_path_matches_fd(path, fd)
            or readback.get("released") is not True
            or readback.get("reconciliation_id") != reconciliation_id
            or readback.get("token") != record.get("token")
        ):
            return {"status": "blocked", "reason": "lock_readback_mismatch", "lock_writes": lock_writes}
        audit["status"] = "completed"
        audit["completed_at"] = time.time()
        _secure_atomic_json(archive, audit)
        return {
            "status": "reconciled",
            "post_id": post_id,
            "reconciliation_id": reconciliation_id,
            "owner_record_sha256": owner_hash,
            "lock_writes": 1,
            "audit_path": str(archive),
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "blocked",
            "reason": "legacy_reconciliation_io_error",
            "error_class": type(exc).__name__,
            "lock_writes": lock_writes,
        }
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
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            created = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockError(f"post {post_id} is already locked") from exc

            fd_stat = os.fstat(fd)
            path_stat = path.stat()
            if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                raise LockError(f"post {post_id} lock file changed during acquisition")
            prior = self._read_legacy_record(fd)
            if created:
                if prior is not None:
                    raise LockError(f"post {post_id} new lock file was not empty")
            else:
                owner_status, owner_reason = classify_lock_owner_record(prior)
                if owner_status != "released":
                    raise LockError(
                        f"post {post_id} lock owner requires explicit reconciliation: "
                        f"{owner_status} ({owner_reason})"
                    )

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
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_nonstandard_json,
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
