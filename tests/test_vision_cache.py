"""Testes do cache de validacao visual (media/vision_cache.py)."""

import tempfile
import unittest
from pathlib import Path

from unicornio_editor.media.vision_cache import (
    cache_key,
    get_cached_decision,
    read_vision_cache,
    set_cached_decision,
    vision_cache_path,
)


class VisionCacheTests(unittest.TestCase):
    def test_cache_key_differs_by_entity_and_version(self):
        url = "https://media.example/keyart.jpg"
        self.assertNotEqual(cache_key(url, "gta 6"), cache_key(url, "redfall"))
        self.assertNotEqual(
            cache_key(url, "gta 6", "v1"), cache_key(url, "gta 6", "v2")
        )
        # Normalizacao: mesmo termo com acento/caixa -> mesma chave.
        self.assertEqual(cache_key(url, "Redfall"), cache_key(url, "redfall"))

    def test_set_and_get_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision = {"status": "MATCH", "confidence": 0.97, "visual_type": "key_art"}
            set_cached_decision(root, "https://media.example/a.webp", "gta 6", decision)
            got = get_cached_decision(root, "https://media.example/a.webp", "gta 6")
            self.assertEqual(got["status"], "MATCH")
            self.assertTrue(vision_cache_path(root).is_file())

    def test_missing_cache_returns_empty_and_none(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(read_vision_cache(root), {})
            self.assertIsNone(get_cached_decision(root, "https://x/a.webp", "gta"))

    def test_different_entity_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_cached_decision(
                root, "https://media.example/a.webp", "gta 6",
                {"status": "MATCH", "confidence": 1.0, "visual_type": "key_art"},
            )
            self.assertIsNone(get_cached_decision(root, "https://media.example/a.webp", "redfall"))


if __name__ == "__main__":
    unittest.main()
