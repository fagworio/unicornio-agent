import unittest

from unicornio_editor.trailer import TrailerError, validate_trailer


class TrailerTests(unittest.TestCase):
    def test_accepts_official_youtube_trailer(self):
        value = validate_trailer({
            "url": "https://www.youtube.com/watch?v=abc123",
            "channel_url": "https://www.youtube.com/@OfficialPublisher",
            "official_source": True,
        })
        self.assertEqual(value["url"], "https://www.youtube.com/watch?v=abc123")

    def test_returns_none_when_trailer_is_not_needed(self):
        self.assertIsNone(validate_trailer(None))

    def test_rejects_untrusted_video_host(self):
        with self.assertRaises(TrailerError):
            validate_trailer({
                "url": "https://random.example/video",
                "channel_url": "https://random.example/channel",
                "official_source": True,
            })

    def test_rejects_non_official_source(self):
        with self.assertRaises(TrailerError):
            validate_trailer({
                "url": "https://www.youtube.com/watch?v=abc123",
                "channel_url": "https://www.youtube.com/@fan",
                "official_source": False,
            })


if __name__ == "__main__":
    unittest.main()
