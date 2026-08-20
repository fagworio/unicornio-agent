import unittest

from unicornio_editor.maintenance import generate_report


class MaintenanceReportTests(unittest.TestCase):
    def test_reports_all_relevant_maintenance_findings_without_writing(self):
        posts = [{
            "id": 42,
            "status": "publish",
            "content": {"raw": '<p>Texto.</p><img src="https://cdn.example/image.jpg">'},
            "meta": {"rank_math_title": "", "rank_math_description": ""},
            "featured_media": 0,
        }]
        report = generate_report(
            posts,
            broken_urls={"https://cdn.example/image.jpg"},
            media_records=[{"id": 9, "post": 0}],
            min_inline_images=2,
        )
        codes = {item["code"] for item in report}
        self.assertIn("broken_image", codes)
        self.assertIn("missing_cta_source", codes)
        self.assertIn("weak_seo", codes)
        self.assertIn("legacy_image_format", codes)
        self.assertIn("missing_featured_media", codes)
        self.assertIn("insufficient_media", codes)
        self.assertIn("orphan_media", codes)

    def test_report_is_empty_for_healthy_post(self):
        posts = [{
            "id": 42,
            "status": "pending",
            "content": {"raw": '<p>Texto.</p><img src="https://site.test/image.webp">'
                '<h3>Confira mais novidades em nosso Portal de '
                '<a href="https://prod.unicorniohater.com.br/noticias/">Notícias!</a></h3>'
                '<em>Fonte: <a href="https://source.test">Source</a></em>'},
            "meta": {
                "rank_math_title": "Título SEO",
                "rank_math_description": "Uma descrição suficientemente longa para demonstrar que o campo SEO está preenchido corretamente e pronto para a análise editorial.",
            },
            "featured_media": 7,
        }]
        self.assertEqual(generate_report(posts), [])


if __name__ == "__main__":
    unittest.main()
