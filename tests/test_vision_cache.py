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
from unicornio_editor.media.vision_policy import vision_cache_subject


class VisionCacheTests(unittest.TestCase):
    def test_work_identity_reuses_cache_across_different_headlines(self):
        first = {"game_name": "Cyberpunk: Edgerunners", "seo": {"title": "Trailer novo"}}
        second = {"game_name": "Cyberpunk: Edgerunners", "seo": {"title": "Elenco confirmado"}}
        self.assertEqual(vision_cache_subject(first), vision_cache_subject(second))

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

    def test_content_hash_survives_url_change_but_not_byte_change(self):
        first_hash = "a" * 64
        changed_hash = "b" * 64
        decision = {
            "status": "MATCH", "confidence": 0.91, "visual_type": "key_art",
            "verdict": "accept", "reason": "modelo confirmou",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_cached_decision(
                root, "https://cdn.test/first.jpg", "Xbox Live", decision,
                image_sha256=first_hash,
            )
            cached = get_cached_decision(
                root, "https://cdn.test/second.webp", "Xbox Live",
                image_sha256=first_hash,
            )
            self.assertEqual(cached, decision)
            self.assertIsNone(get_cached_decision(
                root, "https://cdn.test/second.webp", "Xbox Live",
                image_sha256=changed_hash,
            ))

    def test_featured_policy_does_not_reuse_inline_decision(self):
        image_hash = "d" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_cached_decision(
                root, "https://cdn.test/keyart.webp", "Xbox Live",
                {"status": "MATCH", "confidence": 0.95, "visual_type": "photograph"},
                image_sha256=image_hash, policy="subject-match-v1",
            )
            self.assertIsNone(get_cached_decision(
                root, "https://cdn.test/keyart.webp", "Xbox Live",
                image_sha256=image_hash, policy="featured-key-art-v1",
            ))

    def test_byte_cache_does_not_reuse_url_only_legacy_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_cached_decision(
                root, "https://cdn.test/image.jpg", "Xbox Live",
                {"status": "MATCH", "confidence": 1.0, "visual_type": "other"},
            )
            self.assertIsNone(get_cached_decision(
                root, "https://cdn.test/image.jpg", "Xbox Live",
                image_sha256="c" * 64,
            ))


if __name__ == "__main__":
    unittest.main()
