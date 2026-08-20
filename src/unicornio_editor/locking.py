"""Per-post filesystem locks with expiration and ownership tokens."""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path


class LockError(RuntimeError):
    """Raised when a post lock cannot be acquired safely."""


class LockHandle:
    def __init__(self, path: Path, token: str):
        self.path = path
        self.token = token
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            self._released = True
            return
        if data.get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self._released = True

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, *_exc: object) -> None:
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
        token = secrets.token_urlsafe(24)
        record = {"token": token, "created_at": time.time(), "post_id": post_id}
        for _attempt in range(2):
            try:
                with path.open("x", encoding="utf-8") as handle:
                    json.dump(record, handle)
                return LockHandle(path, token)
            except FileExistsError:
                if not self._is_expired(path):
                    raise LockError(f"post {post_id} is already locked")
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
        raise LockError(f"could not replace expired lock for post {post_id}")

    def _is_expired(self, path: Path) -> bool:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            created_at = float(record["created_at"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return True
        return time.time() - created_at > self.ttl
