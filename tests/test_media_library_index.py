"""Fase 13 — índice local de mídia (deduplicação da Media Library)."""

import tempfile
import threading
import unittest
from pathlib import Path

from unicornio_editor.media import library_index


class LibraryIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_registra_e_encontra_pela_url_exata(self):
        library_index.register(
            self.root, phash="0101", source_url="https://cdn/x.jpg",
            source_page="https://page/x/", subject="metroid prime 4", media_id=777,
        )
        achado = library_index.find_by_source_url(self.root, "https://cdn/x.jpg")
        self.assertIsNotNone(achado)
        self.assertEqual(achado["media_id"], 777)
        self.assertEqual(library_index.count(self.root), 1)

    def test_reconhece_o_mesmo_frame_recomprimido(self):
        library_index.register(self.root, phash="00000000", source_url="https://cdn/a.jpg")
        # 1 bit de diferença = mesma imagem recomprimida
        self.assertIsNotNone(library_index.find_similar(self.root, "00000001"))
        # frame diferente (8 bits) não casa
        self.assertIsNone(library_index.find_similar(self.root, "11111111"))

    def test_encontra_por_subject_para_reuso(self):
        library_index.register(self.root, phash="1010", source_url="https://cdn/a.jpg",
                               subject="Metroid Prime 4")
        self.assertEqual(len(library_index.find_by_subject(self.root, "metroid prime 4")), 1)
        self.assertEqual(library_index.find_by_subject(self.root, "outro assunto"), [])

    def test_registrar_mesma_url_nao_duplica(self):
        for _ in range(3):
            library_index.register(self.root, phash="1111", source_url="https://cdn/a.jpg",
                                   subject="metroid")
        self.assertEqual(library_index.count(self.root), 1)

    def test_indice_ausente_ou_corrompido_nao_quebra(self):
        self.assertEqual(library_index.load_index(self.root), {"entries": []})
        (self.root / "work").mkdir(parents=True, exist_ok=True)
        (self.root / "work" / "media_index.json").write_text("{lixo", encoding="utf-8")
        self.assertEqual(library_index.count(self.root), 0)
        self.assertIsNone(library_index.find_by_source_url(self.root, "https://x/y.jpg"))


    def test_registro_concorrente_nao_perde_entrada(self):
        """Acceptance 8: o media plan roda em 4 threads — nenhuma pode sumir.

        Sem lock, dois workers leem as mesmas N entradas, cada um grava a sua e
        a primeira atualização é perdida (o índice termina menor do que o real).
        """
        def worker(marca):
            for i in range(10):
                library_index.register(
                    self.root, phash=f"{marca}{i}",
                    source_url=f"https://cdn/{marca}/{i}.jpg", subject=f"s{marca}",
                )

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(library_index.count(self.root), 40)

    def test_escrita_e_atomica_e_nao_deixa_temporario(self):
        library_index.register(self.root, phash="1", source_url="https://x/a.jpg")
        temporarios = list((self.root / "work").glob("*.tmp"))
        self.assertEqual(temporarios, [])


if __name__ == "__main__":
    unittest.main()
