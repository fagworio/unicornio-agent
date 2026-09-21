"""P2.8: reconciliação status WP × _hermes_state × artefatos (somente leitura)."""

import tempfile
import unittest
from pathlib import Path

from unicornio_editor.reconcile import reconcile_post, reconcile_state
from unicornio_editor.state import STATE_AWAITING_HUMAN, STATE_READY


def _post(post_id=7, status="pending", state=None, extra_meta=None):
    meta = {"_hermes_state": state} if state else {}
    meta.update(extra_meta or {})
    return {"id": post_id, "status": status, "meta": meta}


class ReconcileTests(unittest.TestCase):
    def test_awaiting_human_sem_status_no_wp_e_divergencia(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            divs = reconcile_post(_post(status="pending", state=STATE_AWAITING_HUMAN), root)
        self.assertEqual([d["code"] for d in divs], ["awaiting_human_status_mismatch"])
        self.assertIn("mover o status", divs[0]["suggested_repair"])

    def test_ready_com_artefato_blocked_obsoleto(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pasta = root / "backups" / "7"
            pasta.mkdir(parents=True)
            (pasta / "editorial.blocked.json").write_text("{}", encoding="utf-8")
            divs = reconcile_post(
                _post(state=STATE_READY, extra_meta={"_hermes_ready_hash": "abc"}), root
            )
        self.assertIn("blocked_artifact_stale", [d["code"] for d in divs])

    def test_ready_sem_hash_e_divergencia(self):
        with tempfile.TemporaryDirectory() as directory:
            divs = reconcile_post(_post(state=STATE_READY), Path(directory))
        self.assertIn("ready_sem_hash", [d["code"] for d in divs])

    def test_post_legado_sem_estado(self):
        with tempfile.TemporaryDirectory() as directory:
            divs = reconcile_post(_post(status="pending", state=None), Path(directory))
        self.assertIn("missing_state_marker", [d["code"] for d in divs])

    def test_post_coerente_nao_gera_divergencia(self):
        with tempfile.TemporaryDirectory() as directory:
            divs = reconcile_post(
                _post(status="awaiting_human", state=STATE_AWAITING_HUMAN), Path(directory)
            )
        self.assertEqual(divs, [])

    def test_reconcile_state_e_read_only(self):
        class FakeClient:
            def list_pending(self, **kwargs):
                return [_post(status="pending", state=STATE_AWAITING_HUMAN)]

        with tempfile.TemporaryDirectory() as directory:
            out = reconcile_state(FakeClient(), None, Path(directory), limit=10)
        self.assertTrue(out["read_only"])
        self.assertEqual(out["scanned"], 1)
        self.assertEqual(out["by_code"].get("awaiting_human_status_mismatch"), 1)


if __name__ == "__main__":
    unittest.main()
