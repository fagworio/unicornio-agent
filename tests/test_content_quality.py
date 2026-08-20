import unittest

from unicornio_editor.content_quality import (
    ContentQualityError,
    minimum_image_count,
    validate_centered_images,
    validate_content_quality,
    word_count,
)


class ContentQualityTests(unittest.TestCase):
    def test_image_thresholds_follow_editorial_policy(self):
        self.assertEqual(minimum_image_count(600), 2)
        self.assertEqual(minimum_image_count(601), 4)
        self.assertEqual(minimum_image_count(1000), 4)
        self.assertEqual(minimum_image_count(1001), 6)
        self.assertEqual(minimum_image_count(1500), 6)

    def test_quality_accepts_aligned_relevant_human_copy(self):
        html = (
            "<h2>O novo filme de Bailarina</h2>"
            "<p>Bailarina acompanha Ana de Armas em uma história de vingança ligada ao universo de John Wick.</p>"
            "<p>A ação e o suspense ajudam a apresentar a personagem e o trailer revela o tom do filme.</p>"
            "<figure class=\"aligncenter\"><img src=\"a.webp\" /></figure>"
            "<figure class=\"aligncenter\"><img src=\"b.webp\" /></figure>"
        )
        result = validate_content_quality(
            html,
            title="Bailarina: Ana de Armas no universo de John Wick",
            focus_keyword="Bailarina",
            image_count=2,
            matched_topics=["filmes"],
            allowed_topics=["filmes", "games"],
            related_terms=["Ana de Armas", "John Wick"],
        )
        self.assertTrue(result["passed"])
        validate_centered_images(html)

    def test_rejects_insufficient_images_for_long_copy(self):
        html = "<p>" + ("Bailarina Ana de Armas John Wick ação suspense. " * 130) + "</p>"
        with self.assertRaises(ContentQualityError):
            validate_content_quality(
                html,
                title="Bailarina Ana de Armas",
                focus_keyword="Bailarina",
                image_count=2,
                matched_topics=["filmes"],
            )

    def test_rejects_unaligned_or_artificial_copy(self):
        html = "<p>Bailarina é importante destacar que este texto fala de filmes.</p>"
        with self.assertRaises(ContentQualityError):
            validate_content_quality(
                html,
                title="Bailarina",
                focus_keyword="Bailarina",
                image_count=2,
                matched_topics=["finanças"],
                allowed_topics=["filmes"],
            )

    def test_rejects_non_centered_images_and_dashes(self):
        html = '<p>Bailarina mostra ação e suspense.</p><img src="a.webp" />'
        with self.assertRaises(ContentQualityError):
            validate_centered_images(html)
        with self.assertRaises(ContentQualityError):
            validate_content_quality(
                '<p>Bailarina — ação e suspense.</p><figure class="aligncenter"><img src="a.webp" /></figure>',
                title="Bailarina",
                focus_keyword="Bailarina",
                image_count=2,
                matched_topics=["filmes"],
            )


if __name__ == "__main__":
    unittest.main()
