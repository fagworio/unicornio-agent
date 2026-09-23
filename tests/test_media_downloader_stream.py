"""Item 1 da auditoria: stream cortado não pode virar imagem truncada.

O post 114414 perdeu 2 imagens com ``IncompleteRead(327270 bytes read)`` do CDN.
O ``IncompleteRead`` herda de ``http.client.HTTPException`` — **não** de
``OSError`` — então escapava do retry do downloader, o arquivo PARCIAL ficava no
disco e o pipeline seguia como se a imagem fosse válida.

Os testes usam ``max_attempts=1`` de propósito: sem retry não há sleep real, e
assim nenhum mock de ``time.sleep`` é necessário (mock global de módulo pode
vazar para outros testes da suíte).
"""

import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest import mock

from unicornio_editor.media.downloader import MediaDownloadError, download_image


def _resposta(content_type="image/jpeg", length="1000"):
    resposta = mock.MagicMock()
    resposta.headers.get_content_type.return_value = content_type
    resposta.headers.get.return_value = length
    resposta.__enter__ = lambda s: resposta
    resposta.__exit__ = lambda *a: False
    return resposta


class StreamCortadoTests(unittest.TestCase):
    def test_incomplete_read_nao_deixa_arquivo_parcial(self):
        destino = Path(tempfile.mkdtemp()) / "img.jpg"
        resposta = _resposta(length="1000")
        resposta.read.side_effect = IncompleteRead(b"123", 877)
        with mock.patch("unicornio_editor.media.downloader.urlopen", return_value=resposta):
            with self.assertRaises(MediaDownloadError):
                download_image("https://cdn.example/img.jpg", destino, max_attempts=1)
        self.assertFalse(destino.exists(), "arquivo parcial ficou no disco")

    def test_corte_silencioso_compara_com_content_length(self):
        """Servidor fecha cedo sem exceção: bytes < Content-Length = falha."""
        destino = Path(tempfile.mkdtemp()) / "img.jpg"
        resposta = _resposta(length="1000")
        leituras = iter([b"x" * 400, b""])
        resposta.read.side_effect = lambda _n: next(leituras, b"")
        with mock.patch("unicornio_editor.media.downloader.urlopen", return_value=resposta):
            with self.assertRaises(MediaDownloadError):
                download_image("https://cdn.example/img.jpg", destino, max_attempts=1)
        self.assertFalse(destino.exists(), "imagem incompleta foi aceita")

    def test_download_completo_continua_funcionando(self):
        """Contraprova: o caminho feliz não regride."""
        destino = Path(tempfile.mkdtemp()) / "img.jpg"
        resposta = _resposta(length="12")
        leituras = iter([b"x" * 12, b""])
        resposta.read.side_effect = lambda _n: next(leituras, b"")
        with mock.patch("unicornio_editor.media.downloader.urlopen", return_value=resposta):
            saida = download_image("https://cdn.example/img.jpg", destino, max_attempts=1)
        self.assertEqual(saida, destino)
        self.assertEqual(destino.read_bytes(), b"x" * 12)


if __name__ == "__main__":
    unittest.main()
