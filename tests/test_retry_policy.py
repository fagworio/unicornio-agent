import datetime
import email.utils
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from unicornio_editor.retry_policy import (
    DEFAULT_EDITORIAL_RETRY_SECONDS,
    clear_editorial_backoff,
    editorial_backoff_status,
    parse_retry_after_seconds,
    record_editorial_backoff,
)


class RetryAfterParsingTests(unittest.TestCase):
    def test_delta_seconds_are_parsed_and_capped(self):
        self.assertEqual(parse_retry_after_seconds("12"), 12)
        self.assertEqual(parse_retry_after_seconds("864000"), 604800)

    def test_http_date_is_converted_relative_to_supplied_clock(self):
        now = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
        retry_date = now + datetime.timedelta(seconds=47)
        value = email.utils.format_datetime(retry_date, usegmt=True)
        self.assertEqual(parse_retry_after_seconds(value, now=now), 47)

    def test_past_http_date_has_zero_delay(self):
        now = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
        retry_date = now - datetime.timedelta(seconds=5)
        value = email.utils.format_datetime(retry_date, usegmt=True)
        self.assertEqual(parse_retry_after_seconds(value, now=now), 0)

    def test_invalid_or_missing_header_has_no_server_delay(self):
        self.assertIsNone(parse_retry_after_seconds(None))
        self.assertIsNone(parse_retry_after_seconds("later"))
        self.assertIsNone(parse_retry_after_seconds("-3"))

    def test_naive_now_is_rejected_for_http_date(self):
        now = datetime.datetime(2026, 11, 20, 12, 0)
        retry_date = datetime.datetime(2026, 11, 20, 12, 1, tzinfo=datetime.timezone.utc)
        value = email.utils.format_datetime(retry_date, usegmt=True)
        self.assertIsNone(parse_retry_after_seconds(value, now=now))

    def test_editorial_backoff_is_persisted_and_expires_without_sleeping(self):
        now = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = record_editorial_backoff(
                root,
                batch_id="batch-rate-limit",
                http_status=429,
                retry_after_seconds=90,
                failure_class="rate_limited",
                now=now,
            )
            active = editorial_backoff_status(root, now=now)
            expired = editorial_backoff_status(root, now=now + datetime.timedelta(seconds=90))

            self.assertEqual(state["batch_id"], "batch-rate-limit")
            self.assertTrue(active["active"])
            self.assertEqual(active["remaining_seconds"], 90)
            self.assertFalse(expired["active"])
            self.assertEqual(expired["batch_id"], "batch-rate-limit")

    def test_missing_retry_after_uses_a_bounded_default(self):
        now = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = record_editorial_backoff(
                root,
                batch_id="batch-default-delay",
                http_status=429,
                retry_after_seconds=None,
                failure_class="rate_limited",
                now=now,
            )
            self.assertEqual(state["retry_after_seconds"], DEFAULT_EDITORIAL_RETRY_SECONDS)
            self.assertEqual(
                editorial_backoff_status(root, now=now)["remaining_seconds"],
                DEFAULT_EDITORIAL_RETRY_SECONDS,
            )

    def test_backoff_clear_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_editorial_backoff(
                root,
                batch_id="batch-clear",
                http_status=429,
                retry_after_seconds=10,
                failure_class="rate_limited",
            )
            clear_editorial_backoff(root)
            clear_editorial_backoff(root)
            self.assertFalse(editorial_backoff_status(root)["active"])

    def test_persisted_cooldown_beyond_maximum_fails_closed(self):
        now = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_editorial_backoff(
                root,
                batch_id="batch-tampered-delay",
                http_status=429,
                retry_after_seconds=30,
                failure_class="rate_limited",
                now=now,
            )
            path = root / "work" / "editorial-provider-backoff.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["not_before"] = (now + datetime.timedelta(days=8)).isoformat()
            path.write_text(json.dumps(value), encoding="utf-8")

            self.assertEqual(editorial_backoff_status(root, now=now)["status"], "invalid")

    def test_boolean_schema_version_is_not_a_valid_persisted_record(self):
        now = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_editorial_backoff(
                root,
                batch_id="batch-bool-schema",
                http_status=429,
                retry_after_seconds=30,
                failure_class="rate_limited",
                now=now,
            )
            path = root / "work" / "editorial-provider-backoff.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["schema_version"] = True
            path.write_text(json.dumps(value), encoding="utf-8")

            self.assertEqual(editorial_backoff_status(root, now=now)["status"], "invalid")

    def test_boolean_http_status_is_not_a_valid_persisted_record(self):
        now = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_editorial_backoff(
                root,
                batch_id="batch-bool-status",
                http_status=429,
                retry_after_seconds=30,
                failure_class="rate_limited",
                now=now,
            )
            path = root / "work" / "editorial-provider-backoff.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["http_status"] = True
            path.write_text(json.dumps(value), encoding="utf-8")

            self.assertEqual(editorial_backoff_status(root, now=now)["status"], "invalid")

    def test_boolean_http_status_cannot_be_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                record_editorial_backoff(
                    Path(directory),
                    batch_id="batch-bool-input",
                    http_status=True,
                    retry_after_seconds=30,
                    failure_class="rate_limited",
                )

    def test_only_rate_limited_failure_class_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                record_editorial_backoff(
                    Path(directory),
                    batch_id="batch-invalid-failure-class",
                    http_status=429,
                    retry_after_seconds=30,
                    failure_class="arbitrary-state",
                )

    def test_unrecognized_persisted_failure_class_fails_closed(self):
        now = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_editorial_backoff(
                root,
                batch_id="batch-invalid-persisted-class",
                http_status=429,
                retry_after_seconds=30,
                failure_class="rate_limited",
                now=now,
            )
            path = root / "work" / "editorial-provider-backoff.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["failure_class"] = "arbitrary-state"
            path.write_text(json.dumps(value), encoding="utf-8")

            self.assertEqual(editorial_backoff_status(root, now=now)["status"], "invalid")

    def test_finalizer_does_not_delete_a_concurrent_newer_429_record(self):
        from unicornio_editor.editorial_provider import complete_editorial_backoff

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "work" / "editorial-provider-backoff.json"
            record_editorial_backoff(
                root,
                batch_id="batch-race",
                http_status=429,
                retry_after_seconds=0,
                failure_class="rate_limited",
            )
            unlink_entered = threading.Event()
            allow_unlink = threading.Event()
            record_started = threading.Event()
            record_finished = threading.Event()
            errors = []
            original_unlink = Path.unlink

            def paused_unlink(path, missing_ok=False):
                if path == marker:
                    unlink_entered.set()
                    if not allow_unlink.wait(timeout=5):
                        raise TimeoutError("test did not release marker unlink")
                return original_unlink(path, missing_ok=missing_ok)

            def finalize():
                try:
                    complete_editorial_backoff(root, "batch-race")
                except Exception as exc:  # noqa: BLE001 - assert thread failures below
                    errors.append(exc)

            def record_new_limit():
                record_started.set()
                try:
                    record_editorial_backoff(
                        root,
                        batch_id="batch-race",
                        http_status=429,
                        retry_after_seconds=300,
                        failure_class="rate_limited",
                    )
                except Exception as exc:  # noqa: BLE001 - assert thread failures below
                    errors.append(exc)
                finally:
                    record_finished.set()

            with mock.patch.object(Path, "unlink", new=paused_unlink):
                finalizer = threading.Thread(target=finalize)
                finalizer.start()
                self.assertTrue(unlink_entered.wait(timeout=5))
                writer = threading.Thread(target=record_new_limit)
                writer.start()
                self.assertTrue(record_started.wait(timeout=5))
                writer_finished_before_unlink = record_finished.wait(timeout=0.2)
                allow_unlink.set()
                finalizer.join(timeout=5)
                writer.join(timeout=5)

            self.assertFalse(finalizer.is_alive())
            self.assertFalse(writer.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(writer_finished_before_unlink)
            status = editorial_backoff_status(root)

        self.assertEqual(status["status"], "active")
        self.assertEqual(status["retry_after_seconds"], 300)

    def test_provider_request_lock_serializes_concurrent_callers(self):
        from unicornio_editor.retry_policy import editorial_provider_request_lock

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempting = threading.Event()
            entered = threading.Event()

            def second_caller():
                attempting.set()
                with editorial_provider_request_lock(root):
                    entered.set()

            with editorial_provider_request_lock(root):
                thread = threading.Thread(target=second_caller)
                thread.start()
                self.assertTrue(attempting.wait(timeout=5))
                entered_while_held = entered.wait(timeout=0.2)

            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertFalse(entered_while_held)
        self.assertTrue(entered.is_set())
