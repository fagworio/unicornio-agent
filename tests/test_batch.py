"""Batch context contracts: independent envelopes and fail-soft items."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unicornio_editor.batch import (
    BatchError,
    load_media_resolve_batch,
    prepare_batch,
    validate_batch_id,
)
from unicornio_editor.config import Config
from unicornio_editor.batch import load_editorial_batch
from unicornio_editor.cli import _apply_editorial_batch


class BatchClient:
    def __init__(self, posts):
        self.posts = posts

    def get_post(self, post_id):
        if post_id not in self.posts:
            raise ValueError(f"post {post_id} ausente")
        return self.posts[post_id]

    def get_media(self, media_id):
        return {
            "id": media_id,
            "source_url": "https://wp.test/uploads/obra.webp",
            "title": {"rendered": "Obra videogame"},
            "alt_text": "Obra videogame",
            "media_details": {"width": 1280, "height": 720},
        }


def _post(post_id, title):
    return {
        "id": post_id,
        "status": "pending",
        "title": {"raw": title},
        "date": "2026-09-24T12:00:00",
        "link": f"https://wp.test/?p={post_id}",
        "content": {"raw": "<article><p>Texto original sobre videogame.</p></article>"},
        "meta": {"original_link": "https://source.test/news"},
        "featured_media": 0,
    }


class BatchContextTests(unittest.TestCase):
    def config(self):
        return Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=True)

    def test_prepare_batch_writes_manifest_and_one_context_per_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = BatchClient({1: _post(1, "Post um"), 2: _post(2, "Post dois")})
            result = prepare_batch(
                client, self.config(), root, [1, 2], batch_id="batch-test-001"
            )

            self.assertEqual(result["batch_id"], "batch-test-001")
            self.assertEqual(result["prepared"], 2)
            self.assertEqual(result["failed"], 0)
            manifest = Path(result["manifest"])
            self.assertTrue(manifest.exists())
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["requested_ids"], [1, 2])
            editorial_input = Path(result["editorial_input"])
            self.assertTrue(editorial_input.exists())
            editorial = json.loads(editorial_input.read_text(encoding="utf-8"))
            self.assertEqual([item["post_id"] for item in editorial["posts"]], [1, 2])
            for post_id in (1, 2):
                context_file = root / "work" / "batches" / "batch-test-001" / f"post-{post_id}.json"
                context = json.loads(context_file.read_text(encoding="utf-8"))
                self.assertEqual(context["post_id"], post_id)
                self.assertEqual(context["batch_id"], "batch-test-001")
                self.assertEqual(context["status"], "pending")
                self.assertIn("cleaned_html", context)
                self.assertIn("requirements", context)
                self.assertTrue((root / "backups" / str(post_id) / "prepared.json").exists())

    def test_prepare_batch_isolates_missing_post_and_keeps_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = BatchClient({1: _post(1, "Post um")})
            result = prepare_batch(
                client, self.config(), root, [1, 999], batch_id="batch-partial"
            )
            self.assertEqual(result["prepared"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["items"][0]["status"], "prepared")
            self.assertEqual(result["items"][1]["status"], "error")
            self.assertTrue(
                (root / "work" / "batches" / "batch-partial" / "post-1.json").exists()
            )

    def test_batch_ids_are_safe(self):
        self.assertEqual(validate_batch_id("batch-abc_01"), "batch-abc_01")
        with self.assertRaises(BatchError):
            validate_batch_id("../outside")

    def test_editorial_batch_rejects_duplicate_ids_before_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "editorial.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "batch_id": "batch-input",
                        "items": [
                            {"post_id": 1, "editorial": {}},
                            {"post_id": 1, "editorial": {}},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(BatchError):
                load_editorial_batch(path)

    def test_editorial_batch_accepts_single_inference_results_with_partial_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "editorial-results.json"
            path.write_text(json.dumps({
                "batch_id": "batch-results",
                "results": [
                    {"post_id": 1, "status": "ok", "editorial": {}},
                    {"post_id": 2, "status": "needs_retry", "reason": "fato"},
                ],
            }), encoding="utf-8")
            batch = load_editorial_batch(path)
            self.assertEqual(batch["items"][1]["status"], "needs_retry")
            self.assertEqual(batch["items"][1]["reason"], "fato")

    def test_media_resolve_batch_is_bounded_and_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media.json"
            path.write_text(json.dumps({
                "batch_id": "media-test",
                "posts": [{"post_id": 1, "subject": "Obra", "needed": 2}],
            }), encoding="utf-8")
            batch = load_media_resolve_batch(path)
            self.assertEqual(batch["posts"][0]["query"], "Obra")
            self.assertEqual(batch["posts"][0]["limit"], 3)

    def test_media_resolve_batch_rejects_more_than_two_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media.json"
            path.write_text(json.dumps({
                "batch_id": "media-test",
                "posts": [
                    {"post_id": 1, "subject": "A"},
                    {"post_id": 2, "subject": "B"},
                    {"post_id": 3, "subject": "C"},
                ],
            }), encoding="utf-8")
            with self.assertRaises(BatchError):
                load_media_resolve_batch(path)

    def test_apply_editorial_batch_isolates_results_per_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = {
                "schema_version": 1,
                "batch_id": "batch-apply-test",
                "items": [
                    {"post_id": 1, "editorial": {}},
                    {"post_id": 2, "editorial": {}},
                ],
            }
            config = self.config()
            fake_result = lambda client, config, root, post_id, payload: {
                "post_id": post_id,
                "status": "ready",
                "dry_run": True,
                "wordpress_changed": False,
                "checklist": {"items": []},
                "media_plan_results": [],
            }
            with mock.patch("unicornio_editor.cli.apply_editorial", side_effect=fake_result):
                result = _apply_editorial_batch(
                    mock.Mock(), config, root, batch, dry_run=True, compact=True
                )
            self.assertEqual(result["processed"], 2)
            self.assertEqual([item["post_id"] for item in result["posts"]], [1, 2])
            self.assertTrue(
                (root / "work" / "batches" / "batch-apply-test" / "apply.manifest.json").exists()
            )

    def test_editorial_batch_keeps_needs_retry_item_out_of_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = {
                "schema_version": 1,
                "batch_id": "batch-partial-editorial",
                "items": [
                    {"post_id": 1, "status": "ok", "editorial": {}},
                    {"post_id": 2, "status": "needs_retry", "reason": "fato inconsistente"},
                ],
            }
            config = self.config()
            fake_result = lambda client, config, root, post_id, payload: {
                "post_id": post_id,
                "status": "ready",
                "dry_run": True,
                "wordpress_changed": False,
                "checklist": {"items": []},
                "media_plan_results": [],
            }
            with mock.patch("unicornio_editor.cli.apply_editorial", side_effect=fake_result) as applied:
                result = _apply_editorial_batch(
                    mock.Mock(), config, root, batch, dry_run=True, compact=True
                )
            self.assertEqual(result["processed"], 2)
            self.assertEqual(result["needs_retry"], 1)
            self.assertEqual(applied.call_count, 1)
            self.assertEqual(result["posts"][1]["status"], "needs_retry")

    def test_retry_same_batch_does_not_reapply_ready_item(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "backups" / "1" / "apply.latest.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"batch_id": "batch-idempotent", "status": "ready"}), encoding="utf-8")
            batch = {
                "schema_version": 1,
                "batch_id": "batch-idempotent",
                "items": [{"post_id": 1, "editorial": {}}],
            }
            with mock.patch("unicornio_editor.cli.apply_editorial") as applied:
                result = _apply_editorial_batch(
                    mock.Mock(), self.config(), root, batch, dry_run=False, compact=True
                )
            self.assertEqual(result["noop"], 1)
            self.assertFalse(applied.called)
            self.assertTrue(result["posts"][0]["idempotent"])


if __name__ == "__main__":
    unittest.main()
