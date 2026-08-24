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
    def test_zero_image_long_copy_is_rejected(self):
        html = "<h2>Bailarina: vingança</h2><p>" + ("Bailarina Ana de Armas John Wick ação suspense. " * 130) + "</p>"
        with self.assertRaises(ContentQualityError):
            validate_content_quality(
                html,
                title="Bailarina Ana de Armas",
                focus_keyword="Bailarina",
                image_count=0,
                matched_topics=["filmes"],
            )

    def test_partial_images_still_require_the_minimum(self):
        html = "<h2>Bailarina: vingança</h2><p>" + ("Bailarina Ana de Armas John Wick ação suspense. " * 130) + "</p>"
        with self.assertRaises(ContentQualityError):
            validate_content_quality(
                html,
                title="Bailarina Ana de Armas",
                focus_keyword="Bailarina",
                image_count=4,
                matched_topics=["filmes"],
            )

    def test_listicle_minimum_via_required_images_parameter(self):
        # A 5-item listicle carries 5 images (one per H2); 2/4/6 would demand
        # 6 for this length, but the checklist passes required_images=5 so the
        # count rule never conflicts with the one-image-per-item rule.
        html = "<h2>Bailarina: vingança</h2><p>" + ("Bailarina Ana de Armas John Wick ação suspense. " * 130) + "</p>"
        result = validate_content_quality(
            html,
            title="5 animes sobre Bailarina",
            focus_keyword="Bailarina",
            image_count=5,
            matched_topics=["filmes"],
            required_images=5,
        )
        self.assertTrue(result["passed"])
        with self.assertRaises(ContentQualityError):
            validate_content_quality(
                html,
                title="5 animes sobre Bailarina",
                focus_keyword="Bailarina",
                image_count=4,
                matched_topics=["filmes"],
                required_images=5,
            )

    def test_focus_keyword_matches_across_punctuation_and_stopwords(self):
        html = "<p>Bass x Machina: Netflix revela as primeiras imagens da nova série de ação.</p>"
        result = validate_content_quality(
            html,
            title="Bass x Machina: Netflix revela primeiras imagens",
            focus_keyword="bass x machina netflix",
            image_count=2,
            matched_topics=["series"],
            allowed_topics=["series"],
        )
        self.assertTrue(result["passed"])
        result = validate_content_quality(
            html,
            title="Bass x Machina: Netflix revela primeiras imagens",
            focus_keyword="bass x machina de netflix",
            image_count=2,
            matched_topics=["series"],
            allowed_topics=["series"],
        )
        self.assertTrue(result["passed"])

    def test_focus_keyword_missing_word_still_rejected(self):
        html = "<p>Bass x Machina: Netflix revela primeiras imagens da nova série.</p>"
        with self.assertRaises(ContentQualityError):
            validate_content_quality(
                html,
                title="Bass x Machina: Netflix revela primeiras imagens",
                focus_keyword="bass x machina netflix trailer",
                image_count=2,
                matched_topics=["series"],
                allowed_topics=["series"],
            )


if __name__ == "__main__":
    unittest.main()
