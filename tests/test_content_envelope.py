"""P0 da auditoria — envelope operacional (JSON do CLI) não pode ser publicado.

O post 114180 publicou literalmente ``{"post_id": ..., "cleaned_html": ...}`` no
corpo: o agente tratou a SAÍDA do comando `content` como texto editorial. Estes
testes cobrem as quatro camadas que agora bloqueiam o caso.
"""

import json
import sys
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


if __name__ == "__main__":
    unittest.main()
