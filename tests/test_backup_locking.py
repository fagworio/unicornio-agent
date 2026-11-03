import json
import multiprocessing
import os
import socket
import threading
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from pathlib import Path

from unicornio_editor.backup import SnapshotError, SnapshotStore
from unicornio_editor.locking import (
    LockError,
    LockManager,
    reconcile_stale_post_lock,
)


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

    def test_context_exit_persists_released_owner_record(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory), ttl=60)
            handle = manager.acquire(42)
            with handle:
                handle.assert_owned()
            record = json.loads(handle.path.read_text(encoding="utf-8"))
            self.assertTrue(record["released"])
            self.assertEqual(record["token"], handle.token)

    def test_context_exit_persists_release_when_work_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory), ttl=60)
            handle = manager.acquire(42)
            with self.assertRaisesRegex(RuntimeError, "work failed"):
                with handle:
                    raise RuntimeError("work failed")
            self.assertTrue(json.loads(handle.path.read_text())["released"])

    def test_release_does_not_overwrite_changed_owner_and_surfaces_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            handle = LockManager(Path(directory), ttl=60).acquire(42)
            changed = {"token": "replacement", "released": False, "pid": 99999999}
            handle._write_record(changed)
            with self.assertRaisesRegex(LockError, "token changed"):
                handle.release()
            self.assertEqual(json.loads(handle.path.read_text()), changed)

    def test_release_surfaces_failure_to_persist_but_drops_kernel_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory), ttl=60)
            handle = manager.acquire(42)
            with mock.patch.object(handle, "_write_record", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(LockError, "release-record persistence failed"):
                    handle.release()
            replacement = manager.acquire(42)
            replacement.release()

    def test_stale_owner_reconciliation_requires_exact_preview_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "work" / "locks" / "42.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(
                json.dumps({
                    "token": "old-secret-token",
                    "post_id": 42,
                    "pid": 99999999,
                    "host": socket.gethostname(),
                    "process_start": "1",
                    "released": False,
                }),
                encoding="utf-8",
            )
            preview = reconcile_stale_post_lock(root, 42)
            assert preview["status"] == "reconciliation_available"
            assert "old-secret-token" not in json.dumps(preview)
            rejected = reconcile_stale_post_lock(
                root, 42, apply=True, expected_sha256="wrong"
            )
            assert rejected["reason"] == "preview_snapshot_mismatch"
            applied = reconcile_stale_post_lock(
                root, 42, apply=True, expected_sha256=preview["owner_record_sha256"]
            )
            assert applied["status"] == "reconciled"
            updated = json.loads(lock_path.read_text(encoding="utf-8"))
            assert updated["released"] is True
            assert updated["reconciliation_id"] == applied["reconciliation_id"]
            archive = Path(applied["audit_path"])
            assert archive.stat().st_mode & 0o777 == 0o600
            audit = json.loads(archive.read_text(encoding="utf-8"))
            assert audit["status"] == "completed"
            assert "token" not in audit["previous_owner_record"]
            duplicate = reconcile_stale_post_lock(
                root, 42, apply=True, expected_sha256=preview["owner_record_sha256"]
            )
            assert duplicate["status"] == "already_reconciled"

    def test_stale_owner_reconciliation_blocks_missing_or_active_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locks = root / "work" / "locks"
            locks.mkdir(parents=True)
            lock_path = locks / "42.lock"
            lock_path.write_text(
                json.dumps({"token": "old", "pid": 1, "released": False}),
                encoding="utf-8",
            )
            assert reconcile_stale_post_lock(root, 42)["reason"] == "owner_host_missing_or_different"
            active = LockManager(locks).acquire(43)
            try:
                busy = reconcile_stale_post_lock(root, 43)
                assert busy["reason"] == "kernel_lock_busy"
            finally:
                active.release()

    def test_stale_owner_reconciliation_fences_pid_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "work" / "locks" / "42.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(
                json.dumps({
                    "token": "old-owner",
                    "post_id": 42,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "process_start": "different-start-time",
                    "released": False,
                }),
                encoding="utf-8",
            )
            preview = reconcile_stale_post_lock(root, 42)
            assert preview["owner_process_status"] == "pid_reused_after_owner_exit"

    def test_concurrent_stale_lock_reconciliation_has_one_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "work" / "locks" / "42.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(
                json.dumps({
                    "token": "old-owner",
                    "post_id": 42,
                    "pid": 99999999,
                    "host": socket.gethostname(),
                    "process_start": "1",
                    "released": False,
                }),
                encoding="utf-8",
            )
            preview = reconcile_stale_post_lock(root, 42)
            barrier = threading.Barrier(2)

            def apply_reconcile():
                barrier.wait(timeout=2)
                return reconcile_stale_post_lock(
                    root,
                    42,
                    apply=True,
                    expected_sha256=preview["owner_record_sha256"],
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: apply_reconcile(), range(2)))
            assert sum(result["status"] == "reconciled" for result in results) == 1
            assert {result["status"] for result in results} <= {
                "reconciled", "already_reconciled", "blocked"
            }

    def test_interrupted_stale_lock_reconciliation_resumes_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "work" / "locks" / "42.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(
                json.dumps({
                    "token": "old-owner",
                    "post_id": 42,
                    "pid": 99999999,
                    "host": socket.gethostname(),
                    "process_start": "1",
                    "released": False,
                }),
                encoding="utf-8",
            )
            preview = reconcile_stale_post_lock(root, 42)
            from unicornio_editor import locking

            original = locking._secure_atomic_json
            calls = 0

            def fail_completion(path, value):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated crash after lock record update")
                return original(path, value)

            with mock.patch.object(locking, "_secure_atomic_json", fail_completion):
                interrupted = reconcile_stale_post_lock(
                    root,
                    42,
                    apply=True,
                    expected_sha256=preview["owner_record_sha256"],
                )
            assert interrupted["status"] == "blocked"
            assert json.loads(lock_path.read_text())["released"] is True
            resumed = reconcile_stale_post_lock(
                root,
                42,
                apply=True,
                expected_sha256=preview["owner_record_sha256"],
            )
            assert resumed["status"] == "already_reconciled"
            audit_path = root / "work" / "lock-reconciliations" / f"42-{resumed['reconciliation_id']}.json"
            assert json.loads(audit_path.read_text())["status"] == "completed"

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
            released_record = json.loads(lock_path.read_text())
            self.assertTrue(released_record["released"])
            self.assertFalse(handle.refresh())
            self.assertEqual(json.loads(lock_path.read_text()), released_record)

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
            with self.assertRaisesRegex(LockError, "replaced while held"):
                handle.release()
            self.assertEqual(
                json.loads(lock_path.read_text(encoding="utf-8")),
                {"token": "new-owner", "released": True},
            )


if __name__ == "__main__":
    unittest.main()
