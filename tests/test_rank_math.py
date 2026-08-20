import unittest

from unicornio_editor.seo.rank_math import RankMathError, build_meta


class RankMathTests(unittest.TestCase):
    def test_maps_editorial_seo_to_rank_math_meta(self):
        seo = {
            "title": "Título SEO sobre videogame",
            "meta_description": "Uma descrição SEO suficientemente longa sobre videogame, plataformas, contexto e detalhes importantes para o leitor compreender a notícia.",
            "focus_keyword": "videogame",
        }
        result = build_meta(seo)
        self.assertEqual(result["rank_math_title"], seo["title"])
        self.assertEqual(result["rank_math_description"], seo["meta_description"])
        self.assertEqual(result["rank_math_focus_keyword"], "videogame")

    def test_preserves_unmanaged_meta(self):
        seo = {
            "title": "Título SEO sobre videogame",
            "meta_description": "Uma descrição SEO suficientemente longa sobre videogame, plataformas, contexto e detalhes importantes para o leitor compreender a notícia.",
            "focus_keyword": "videogame",
        }
        result = build_meta(seo, {"rank_math_robots": ["index"], "custom": "keep"})
        self.assertEqual(result["custom"], "keep")
        self.assertEqual(result["rank_math_robots"], ["index"])

    def test_rejects_invalid_seo_title(self):
        seo = {
            "title": "x" * 66,
            "meta_description": "Uma descrição SEO suficientemente longa sobre videogame, plataformas, contexto e detalhes importantes para o leitor compreender a notícia.",
            "focus_keyword": "videogame",
        }
        with self.assertRaises(RankMathError):
            build_meta(seo)


if __name__ == "__main__":
    unittest.main()
