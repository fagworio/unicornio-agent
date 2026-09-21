"""P0 da auditoria — envelope operacional (JSON do CLI) não pode ser publicado.

O post 114180 publicou literalmente ``{"post_id": ..., "cleaned_html": ...}`` no
corpo: o agente tratou a SAÍDA do comando `content` como texto editorial. Estes
testes cobrem as quatro camadas que agora bloqueiam o caso.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

# o helper de payload válido vive no test_workflow (mesmo diretório, sem pacote)
sys.path.insert(0, str(Path(__file__).parent))
from test_workflow import editorial_payload  # noqa: E402

from unicornio_editor.content_quality import (
    looks_like_operational_envelope,
    unwrap_operational_envelope,
)
from unicornio_editor.editorial_schema import EditorialValidationError, validate_editorial

ENVELOPE = json.dumps({
    "post_id": 114180,
    "status": "publish",
    "cleaned_html": "<p>Carrie Fisher e a saga de Star Wars.</p>",
    "original_link": "https://fonte.example/carrie",
    "word_count": 8,
})


class DeteccaoTests(unittest.TestCase):
    def test_envelope_do_cli_e_reconhecido(self):
        self.assertTrue(looks_like_operational_envelope(ENVELOPE))
        self.assertTrue(looks_like_operational_envelope("  " + ENVELOPE + "  "))

    def test_html_editorial_nao_e_envelope(self):
        self.assertFalse(looks_like_operational_envelope("<p>Texto normal do post.</p>"))
        self.assertFalse(looks_like_operational_envelope(""))
        # JSON qualquer com UMA chave não é um envelope de comando
        self.assertFalse(looks_like_operational_envelope('{"foo": "bar"}'))

    def test_desembrulha_devolve_o_html_real(self):
        self.assertEqual(
            unwrap_operational_envelope(ENVELOPE),
            "<p>Carrie Fisher e a saga de Star Wars.</p>",
        )
        self.assertEqual(unwrap_operational_envelope("<p>ok</p>"), "<p>ok</p>")

    def test_envelope_sem_conteudo_aproveitavel_vira_vazio(self):
        self.assertEqual(unwrap_operational_envelope('{"post_id": 1, "status": "pending"}'), "")


class ValidacaoTests(unittest.TestCase):
    def _payload(self, cleaned):
        payload = editorial_payload()
        payload["cleaned_html"] = cleaned
        return payload

    def test_validate_editorial_rejeita_envelope_no_corpo(self):
        with self.assertRaises(EditorialValidationError) as ctx:
            validate_editorial(self._payload(ENVELOPE))
        self.assertIn("envelope", str(ctx.exception).lower())

    def test_validate_editorial_aceita_html_normal(self):
        validate_editorial(self._payload("<p>Conteudo editorial normal.</p>"))


class CamadasReaisTests(unittest.TestCase):
    """P0: as camadas no CAMINHO REAL (não só nos helpers).

    A auditoria apontou que os 6 testes anteriores cobriam apenas
    `looks_like_operational_envelope`, `unwrap_operational_envelope` e
    `validate_editorial` — e foi por isso que o CI ficou verde com o
    `get_cleaned_content()` sem usar o unwrap.
    """

    def _post_com(self, conteudo):
        return {
            "id": 114180,
            "status": "pending",
            "title": {"raw": "Carrie Fisher"},
            "content": {"raw": conteudo},
            "meta": {},
        }

    def test_get_cleaned_content_desembrulha_o_envelope(self):
        from unicornio_editor.workflow import get_cleaned_content

        class FakeClient:
            def __init__(self, post):
                self.post = post

            def get_post(self, post_id):
                return self.post

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(self._post_com(ENVELOPE))
            dados = get_cleaned_content(client, Path(directory), 114180)
        # o HTML real volta, não o JSON do comando
        self.assertIn("Carrie Fisher e a saga", dados["cleaned_html"])
        self.assertNotIn("post_id", dados["cleaned_html"])

    def test_publish_de_envelope_vira_blocked(self):
        """O fast-path `publish POST_ID` também bloqueia (invariância no nível
        mais baixo: antes só o loop do cron tinha o sanity)."""
        from unicornio_editor.config import load_config
        from unicornio_editor.workflow import publish_post

        class FakeClient:
            def __init__(self, post):
                self.post = post
                self.updated = []

            def get_post(self, post_id):
                return self.post

            def update_post(self, post_id, payload):
                self.updated.append((post_id, payload))
                return self.post

            def move_to_status(self, post_id, status):
                return None

        with tempfile.TemporaryDirectory() as directory:
            cliente = FakeClient(self._post_com(ENVELOPE))
            resultado = publish_post(cliente, load_config(), Path(directory), 114180)
        self.assertEqual(resultado["status"], "blocked")
        self.assertFalse(resultado["wordpress_changed"])
        self.assertIn("envelope", str(resultado.get("reason") or "").lower())
        # o estado foi gravado (não é skip silencioso)
        self.assertEqual(resultado.get("state"), "blocked")

    def test_hamming_conta_bits_nao_digitos_hex(self):
        """P1 da auditoria: o pHash é hexadecimal — '0' vs 'f' são 4 bits."""
        from unicornio_editor.media.library_index import hamming

        self.assertEqual(hamming("0", "f"), 4)
        self.assertEqual(hamming("00", "0f"), 4)
        self.assertEqual(hamming("abcd", "abcd"), 0)
        self.assertEqual(hamming("00000000", "00000001"), 1)
        self.assertEqual(hamming("", "ab"), 999)
        self.assertEqual(hamming("zz", "ab"), 999)


if __name__ == "__main__":
    unittest.main()
