import json
import threading
import tempfile
import time
import unittest
from unittest import mock
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

    def test_lock_handle_does_not_refresh_after_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory), ttl=60)
            handle = manager.acquire(42)
            with handle:
                self.assertTrue(handle.refresh())
            self.assertFalse(handle.refresh())

    def test_refresh_and_release_are_serialized_during_file_write(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory), ttl=60)
            handle = manager.acquire(42)
            lock_path = Path(directory) / "42.lock"
            write_started = threading.Event()
            continue_write = threading.Event()
            release_done = threading.Event()
            original_write_text = Path.write_text

            def blocking_write_text(path, data, *args, **kwargs):
                if path == lock_path:
                    write_started.set()
                    assert continue_write.wait(timeout=2)
                return original_write_text(path, data, *args, **kwargs)

            with mock.patch.object(Path, "write_text", blocking_write_text):
                refresh_thread = threading.Thread(target=handle.refresh)
                refresh_thread.start()
                self.assertTrue(write_started.wait(timeout=2))

                release_thread = threading.Thread(
                    target=lambda: (handle.release(), release_done.set())
                )
                release_thread.start()
                self.assertFalse(release_done.wait(timeout=0.05))

                continue_write.set()
                refresh_thread.join(timeout=2)
                release_thread.join(timeout=2)

            self.assertFalse(refresh_thread.is_alive())
            self.assertFalse(release_thread.is_alive())
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
