import json
import tempfile
import time
import unittest
from pathlib import Path

from unicornio_editor.backup import SnapshotError, SnapshotStore
from unicornio_editor.locking import LockError, LockManager


class SnapshotAndLockTests(unittest.TestCase):
    def test_snapshot_is_atomic_json_and_can_be_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory))
            payload = {"id": 42, "status": "pending", "content": {"raw": "before"}}
            path = store.save(42, payload)
            self.assertEqual(store.load(path), payload)
            self.assertEqual(store.latest(42), payload)
            self.assertNotIn(".tmp", path.name)

    def test_snapshot_rejects_invalid_post_id(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SnapshotError):
                SnapshotStore(Path(directory)).save(0, {})

    def test_lock_blocks_second_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory), ttl=60)
            first = manager.acquire(42)
            with self.assertRaises(LockError):
                manager.acquire(42)
            first.release()
            second = manager.acquire(42)
            second.release()

    def test_expired_lock_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory), ttl=1)
            first = manager.acquire(42)
            first.release()
            lock_path = Path(directory) / "42.lock"
            lock_path.write_text(json.dumps({"created_at": time.time() - 10}))
            replacement = manager.acquire(42)
            replacement.release()


if __name__ == "__main__":
    unittest.main()
