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
    _process_start_identity,
    _read_private_audit,
    classify_lock_owner_record,
    reconcile_legacy_post_lock,
    reconcile_stale_post_lock,
)
from unicornio_editor.pipeline_v2.cycle_investigation import _probe_lock


def _hold_post_lock(lock_root: str, acquired, release) -> None:
    handle = LockManager(Path(lock_root), ttl=1).acquire(42)
    acquired.set()
    release.wait(timeout=15)
    handle.release()


def _apply_stale_lock_preview(root: Path, post_id: int, preview: dict, **extra):
    return reconcile_stale_post_lock(
        root,
        post_id,
        apply=True,
        expected_sha256=preview["owner_record_sha256"],
        expected_device=preview["owner_device"],
        expected_inode=preview["owner_inode"],
        expected_mtime_ns=preview["owner_mtime_ns"],
        expected_bytes_sha256=preview["owner_record_bytes_sha256"],
        **extra,
    )


def _legacy_external_proof(preview: dict) -> dict:
    return {
        key: preview[key]
        for key in (
            "post_id", "owner_record_sha256", "owner_record_bytes_sha256",
            "owner_device", "owner_inode", "owner_mtime_ns",
        )
    } | {
        "owner_source_verified": True,
        "writer_processes_stopped": True,
        "proof_reference_sha256": __import__("hashlib").sha256(b"external-reviewed-evidence").hexdigest(),
    }


class SnapshotAndLockTests(unittest.TestCase):
    def test_private_audit_preserves_io_error_before_payload_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o600)

            original_close = os.close
            closed_fds: list[int] = []

            def close_and_record(fd: int) -> None:
                closed_fds.append(fd)
                original_close(fd)

            with mock.patch(
                "unicornio_editor.locking.os.fstat",
                side_effect=OSError("injected fstat failure"),
            ), mock.patch("unicornio_editor.locking.os.close", side_effect=close_and_record):
                with self.assertRaisesRegex(OSError, "injected fstat failure"):
                    _read_private_audit(path)

            self.assertEqual(len(closed_fds), 1)

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

    def test_expired_unreleased_legacy_lock_is_not_replaced_by_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory), ttl=1)
            lock_path = Path(directory) / "42.lock"
            legacy = {"created_at": time.time() - 10, "released": False}
            lock_path.write_text(json.dumps(legacy))
            before = lock_path.read_bytes()
            with self.assertRaisesRegex(LockError, "legacy_unverifiable"):
                manager.acquire(42)
            self.assertEqual(lock_path.read_bytes(), before)

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
            with self.assertRaisesRegex(LockError, "live"):
                manager.acquire(42)

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
                root,
                42,
                apply=True,
                expected_sha256="wrong",
                expected_device=preview["owner_device"],
                expected_inode=preview["owner_inode"],
                expected_mtime_ns=preview["owner_mtime_ns"],
                expected_bytes_sha256=preview["owner_record_bytes_sha256"],
            )
            assert rejected["reason"] == "preview_snapshot_mismatch"
            applied = _apply_stale_lock_preview(root, 42, preview)
            assert applied["status"] == "reconciled"
            updated = json.loads(lock_path.read_text(encoding="utf-8"))
            assert updated["released"] is True
            assert updated["reconciliation_id"] == applied["reconciliation_id"]
            archive = Path(applied["audit_path"])
            assert archive.stat().st_mode & 0o777 == 0o600
            audit = json.loads(archive.read_text(encoding="utf-8"))
            assert audit["status"] == "completed"
            assert "token" not in audit["previous_owner_record"]
            duplicate = _apply_stale_lock_preview(root, 42, preview)
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
                    expected_device=preview["owner_device"],
                    expected_inode=preview["owner_inode"],
                    expected_mtime_ns=preview["owner_mtime_ns"],
                    expected_bytes_sha256=preview["owner_record_bytes_sha256"],
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
                    expected_device=preview["owner_device"],
                    expected_inode=preview["owner_inode"],
                    expected_mtime_ns=preview["owner_mtime_ns"],
                    expected_bytes_sha256=preview["owner_record_bytes_sha256"],
                )
            assert interrupted["status"] == "blocked"
            assert json.loads(lock_path.read_text())["released"] is True
            resumed = _apply_stale_lock_preview(root, 42, preview)
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

    def test_legacy_owner_records_for_reported_posts_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_root = root / "work" / "locks"
            lock_root.mkdir(parents=True)
            post_ids = (115102, 115481, 115786, 115892, 115975)
            for post_id in post_ids:
                path = lock_root / f"{post_id}.lock"
                record = {
                    "token": f"legacy-{post_id}",
                    "pid": 1942977,
                    "released": False,
                }
                path.write_text(json.dumps(record), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaisesRegex(LockError, "legacy_unverifiable"):
                    LockManager(lock_root).acquire(post_id)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(_probe_lock(path), "legacy_unverifiable")
                proposal = reconcile_legacy_post_lock(root, post_id)
                assert proposal["status"] == "legacy_recovery_proposed"
                assert proposal["automatic_recovery"] is False
                denied = reconcile_legacy_post_lock(
                    root,
                    post_id,
                    apply=True,
                    preview=proposal,
                    external_proof=None,
                    authorized_by="reviewer",
                    authorization_id="auth-legacy-review",
                )
                assert denied["status"] == "blocked"
                self.assertEqual(path.read_bytes(), before)

    def test_legacy_lock_reconciliation_requires_external_proof_and_human_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "work" / "locks" / "115975.lock"
            lock_path.parent.mkdir(parents=True)
            original = {
                "token": "legacy-secret",
                "post_id": 115975,
                "pid": 1942977,
                "released": False,
            }
            lock_path.write_text(json.dumps(original), encoding="utf-8")
            preview = reconcile_legacy_post_lock(root, 115975)
            proof = _legacy_external_proof(preview)

            result = reconcile_legacy_post_lock(
                root,
                115975,
                apply=True,
                preview=preview,
                external_proof=proof,
                authorized_by="operator-reviewer",
                authorization_id="manual-auth-115975",
            )

            assert result["status"] == "reconciled"
            assert result["lock_writes"] == 1
            updated = json.loads(lock_path.read_text(encoding="utf-8"))
            assert updated["released"] is True
            assert updated["token"] == original["token"]
            assert updated["reconciliation_id"] == result["reconciliation_id"]
            audit_path = Path(result["audit_path"])
            assert audit_path.stat().st_mode & 0o777 == 0o600
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            assert audit["status"] == "completed"
            assert audit["external_proof"]["writer_processes_stopped"] is True
            assert "token" not in audit["previous_owner_record"]
            assert "legacy-secret" not in audit_path.read_text(encoding="utf-8")
            replacement = LockManager(lock_path.parent).acquire(115975)
            replacement.release()

    def test_legacy_lock_reconciliation_rejects_changed_snapshot_and_weak_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "work" / "locks" / "115102.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(json.dumps({"token": "legacy", "pid": 1942977, "released": False}))
            preview = reconcile_legacy_post_lock(root, 115102)
            proof = _legacy_external_proof(preview)
            weak_proof = dict(proof, writer_processes_stopped=False)
            denied = reconcile_legacy_post_lock(
                root, 115102, apply=True, preview=preview, external_proof=weak_proof,
                authorized_by="reviewer", authorization_id="manual-auth",
            )
            assert denied["reason"] == "external_proof_incomplete"
            before = lock_path.read_bytes()
            info = lock_path.stat()
            os.utime(lock_path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
            changed = reconcile_legacy_post_lock(
                root, 115102, apply=True, preview=preview, external_proof=proof,
                authorized_by="reviewer", authorization_id="manual-auth",
            )
            assert changed["reason"] == "preview_snapshot_mismatch"
            assert lock_path.read_bytes() == before

    def test_legacy_lock_reconciliation_retries_after_audit_finalize_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "work" / "locks" / "115481.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(json.dumps({"token": "legacy", "pid": 1942977, "released": False}))
            preview = reconcile_legacy_post_lock(root, 115481)
            proof = _legacy_external_proof(preview)
            from unicornio_editor import locking

            original_write = locking._secure_atomic_json
            calls = 0

            def fail_final_audit(path, payload):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated audit completion interruption")
                return original_write(path, payload)

            with mock.patch.object(locking, "_secure_atomic_json", side_effect=fail_final_audit):
                interrupted = reconcile_legacy_post_lock(
                    root, 115481, apply=True, preview=preview, external_proof=proof,
                    authorized_by="reviewer", authorization_id="manual-auth-retry",
                )
            assert interrupted["status"] == "blocked"
            assert interrupted["lock_writes"] == 1
            assert json.loads(lock_path.read_text())["released"] is True
            resumed = reconcile_legacy_post_lock(
                root, 115481, apply=True, preview=preview, external_proof=proof,
                authorized_by="reviewer", authorization_id="manual-auth-retry",
            )
            assert resumed["status"] == "already_reconciled"
            audit_path = root / "work" / "lock-reconciliations" / f"115481-legacy-{resumed['reconciliation_id']}.json"
            assert json.loads(audit_path.read_text())["status"] == "completed"

    def test_shared_lock_classification_distinguishes_released_live_dead_and_ambiguous(self):
        base = {
            "token": "owner-token",
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "process_start": _process_start_identity(os.getpid()),
            "released": False,
        }
        assert classify_lock_owner_record({**base, "released": True})[0] == "released"
        assert classify_lock_owner_record(base)[0] == "live"
        assert classify_lock_owner_record({**base, "host": "different-host"})[0] == "legacy_unverifiable"
        assert classify_lock_owner_record({**base, "process_start": None})[0] == "legacy_unverifiable"
        assert classify_lock_owner_record({**base, "pid": 99999999})[0] == "dead_proven"
        assert classify_lock_owner_record({"token": "t", "released": "false"})[0] == "corrupt"
        assert classify_lock_owner_record({"released": True}) == ("corrupt", "released_owner_token_missing")

    def test_released_owner_without_token_is_not_reacquired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "42.lock"
            lock_path.write_text(json.dumps({"released": True}), encoding="utf-8")
            before = lock_path.read_bytes()

            with self.assertRaisesRegex(LockError, "corrupt"):
                LockManager(root).acquire(42)

            self.assertEqual(lock_path.read_bytes(), before)

    def test_reconciliation_blocks_tampered_audit_before_reporting_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "work" / "locks" / "42.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(json.dumps({
                "token": "owner-token", "post_id": 42, "pid": 99999999,
                "host": socket.gethostname(), "process_start": "1", "released": False,
            }), encoding="utf-8")
            preview = reconcile_stale_post_lock(root, 42)
            result = _apply_stale_lock_preview(root, 42, preview)
            assert result["status"] == "reconciled"
            lock_before = lock_path.read_bytes()
            audit_path = Path(result["audit_path"])
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit["previous_owner_record"]["token_sha256"] = "0" * 64
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            audit_path.chmod(0o600)

            blocked = reconcile_stale_post_lock(root, 42, apply=False)

            assert blocked == {
                "status": "blocked", "reason": "reconciliation_audit_mismatch", "lock_writes": 0
            }
            assert lock_path.read_bytes() == lock_before

    def test_acquire_rejects_corrupt_symlink_and_changed_inode_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LockManager(root)
            corrupt = root / "50.lock"
            corrupt.write_bytes(b'{"token":')
            original = corrupt.read_bytes()
            with self.assertRaises(LockError):
                manager.acquire(50)
            self.assertEqual(corrupt.read_bytes(), original)

            duplicate_keys = root / "52.lock"
            duplicate_keys.write_bytes(b'{"token":"t","released":false,"released":true}')
            duplicate_before = duplicate_keys.read_bytes()
            with self.assertRaises(LockError):
                manager.acquire(52)
            self.assertEqual(duplicate_keys.read_bytes(), duplicate_before)

            target = root / "target.lock"
            target.write_text(json.dumps({"token": "x", "released": True}), encoding="utf-8")
            link = root / "51.lock"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                manager.acquire(51)
            assert _probe_lock(link) == "ambiguous_symlink"

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

    def test_crashed_process_lock_requires_previewed_reconciliation_before_reacquire(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_root = root / "work" / "locks"
            lock_root.mkdir(parents=True)
            acquired = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_post_lock,
                args=(str(lock_root), acquired, release),
            )
            process.start()
            self.assertTrue(acquired.wait(timeout=5), "lock holder process did not start")
            process.terminate()
            process.join(timeout=5)
            self.assertFalse(process.is_alive())
            preview = reconcile_stale_post_lock(root, 42)
            assert preview["status"] == "reconciliation_available"
            applied = _apply_stale_lock_preview(root, 42, preview)
            assert applied["status"] == "reconciled"
            replacement = LockManager(lock_root, ttl=60).acquire(42)
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
