import json
import multiprocessing
import os
import threading
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from unicornio_editor.backup import SnapshotError, SnapshotStore
from unicornio_editor.locking import LockError, LockManager


def _hold_post_lock(lock_root: str, acquired, release) -> None:
    handle = LockManager(Path(lock_root), ttl=1).acquire(42)
    acquired.set()
    release.wait(timeout=15)
    handle.release()


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

    def test_recent_uninitialized_legacy_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory), ttl=60)
            lock_path = Path(directory) / "42.lock"
            lock_path.write_text("", encoding="utf-8")
            with self.assertRaises(LockError):
                manager.acquire(42)

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
            original_write_record = handle._write_record

            def blocking_write_record(data):
                if data.get("released") is not True:
                    write_started.set()
                    assert continue_write.wait(timeout=2)
                return original_write_record(data)

            with mock.patch.object(handle, "_write_record", blocking_write_record):
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
            self.assertTrue(lock_path.exists())
            self.assertTrue(json.loads(lock_path.read_text())["released"])

    def test_separate_process_cannot_acquire_an_active_post_lock(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            acquired = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_post_lock,
                args=(directory, acquired, release),
            )
            process.start()
            try:
                self.assertTrue(acquired.wait(timeout=5), "lock holder process did not start")
                # A stale JSON timestamp must not override the live kernel
                # lock held by another process.
                time.sleep(1.1)
                with self.assertRaises(LockError):
                    LockManager(Path(directory), ttl=1).acquire(42)
            finally:
                release.set()
                process.join(timeout=5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)

    def test_crashed_process_lock_is_reclaimed_without_waiting_for_ttl(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            acquired = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_post_lock,
                args=(directory, acquired, release),
            )
            process.start()
            self.assertTrue(acquired.wait(timeout=5), "lock holder process did not start")
            process.terminate()
            process.join(timeout=5)
            self.assertFalse(process.is_alive())
            replacement = LockManager(Path(directory), ttl=60).acquire(42)
            replacement.release()

    def test_replaced_lock_path_fences_old_owner_and_preserves_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory), ttl=60)
            handle = manager.acquire(42)
            lock_path = Path(directory) / "42.lock"
            replacement_path = Path(directory) / "replacement.lock"
            replacement_path.write_text('{"token":"new-owner","released":true}', encoding="utf-8")
            os.replace(replacement_path, lock_path)

            with self.assertRaises(LockError):
                handle.assert_owned()
            handle.release()
            self.assertEqual(
                json.loads(lock_path.read_text(encoding="utf-8")),
                {"token": "new-owner", "released": True},
            )


if __name__ == "__main__":
    unittest.main()
