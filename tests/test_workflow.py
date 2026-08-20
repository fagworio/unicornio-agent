import json
import tempfile
import unittest
from pathlib import Path

from unicornio_editor.config import Config
from unicornio_editor.workflow import apply_editorial, prepare_post


def editorial_payload(decision="process"):
    return {
        "site_relevance": {
            "decision": decision,
            "confidence": 0.99,
            "reason": "Teste",
            "matched_topics": ["games"] if decision == "process" else [],
        },
        "cleaned_html": "<p>Texto revisado.</p>",
        "seo": {
            "title": "Título sobre videogame e lançamento importante",
            "meta_description": "Uma descrição suficientemente longa sobre o conteúdo de videogame, seus detalhes, plataformas e contexto para o leitor entender a notícia.",
            "focus_keyword": "videogame",
        },
        "media_plan": [],
        "needs_trailer": False,
        "trailer_url": None,
    }


class FakeClient:
    def __init__(self, post):
        self.post = post
        self.updated = []

    def get_post(self, post_id):
        return self.post

    def update_post(self, post_id, payload):
        self.updated.append((post_id, payload))
        return {"id": post_id, "status": "pending", **payload}


class WorkflowTests(unittest.TestCase):
    def config(self, dry_run):
        return Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=dry_run)

    def post(self):
        return {
            "id": 42,
            "status": "pending",
            "content": {"raw": "<article><p>Original.</p></article>"},
            "meta": {"original_link": "https://source.example/news"},
        }

    def test_prepare_creates_snapshot_and_cleans_content(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            report = prepare_post(client, Path(directory), 42)
            self.assertEqual(report["post_id"], 42)
            self.assertEqual(report["cleaned_html"], "<p>Original.</p>")
            self.assertTrue(Path(report["backup"]).exists())
            self.assertEqual(client.updated, [])

    def test_apply_skip_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload("skip"))
            self.assertFalse(report["wordpress_changed"])
            self.assertEqual(client.updated, [])

    def test_apply_processes_pending_without_sending_status(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(False), Path(directory), 42, editorial_payload())
            self.assertTrue(report["wordpress_changed"])
            self.assertNotIn("status", client.updated[0][1])
            self.assertIn("Portal de", client.updated[0][1]["content"]["raw"])

    def test_apply_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self.post())
            report = apply_editorial(client, self.config(True), Path(directory), 42, editorial_payload())
            self.assertFalse(report["wordpress_changed"])
            self.assertEqual(report["dry_run"], True)
            self.assertEqual(client.updated, [])


if __name__ == "__main__":
    unittest.main()
