import unittest
from unittest import mock

from unicornio_editor.trailer import (
    TrailerError,
    build_trailer_html,
    find_game_trailer,
    find_game_trailer_with_status,
    validate_trailer,
)


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


class TrailerDiscoveryTests(unittest.TestCase):
    def _candidate(self, video_id="abcDEF12345", title="Hellraiser: Revival - Official Trailer", channel="Boss Team Games"):
        return {"video_id": video_id, "title": title, "channel": channel}

    def _oembed(self):
        return {
            "title": "Hellraiser: Revival - Official Trailer | Boss Team Games",
            "author_name": "Boss Team Games",
            "author_url": "https://www.youtube.com/@BossTeamGames",
            "thumbnail_url": "https://i.ytimg.com/vi/abcDEF12345/hqdefault.jpg",
        }

    def test_finds_relevant_trailer(self):
        with mock.patch("unicornio_editor.trailer._search_youtube", return_value=[self._candidate()]) as search, mock.patch(
            "unicornio_editor.trailer._fetch_oembed", return_value=self._oembed()
        ) as oembed:
            result = find_game_trailer("Clive Barker's Hellraiser: Revival")
        search.assert_called_once()
        oembed.assert_called_once()
        self.assertEqual(result["video_id"], "abcDEF12345")
        self.assertEqual(result["watch_url"], "https://www.youtube.com/watch?v=abcDEF12345")
        self.assertEqual(result["embed_url"], "https://www.youtube-nocookie.com/embed/abcDEF12345")
        self.assertIn("Boss Team Games", result["author_name"])

    def test_skips_videos_without_trailer_in_title(self):
        candidates = [
            self._candidate(video_id="aaaAAA11111", title="Hellraiser: Revival - Let's Play #1"),
            self._candidate(video_id="bbbBBB22222", title="Hellraiser: Revival - Official Trailer"),
        ]
        with mock.patch("unicornio_editor.trailer._search_youtube", return_value=candidates), mock.patch(
            "unicornio_editor.trailer._fetch_oembed", return_value=self._oembed()
        ) as oembed:
            result = find_game_trailer("Hellraiser: Revival")
        self.assertEqual(result["video_id"], "bbbBBB22222")
        self.assertEqual(oembed.call_count, 1)

    def test_skips_videos_about_another_game(self):
        candidates = [
            self._candidate(video_id="aaaAAA11111", title="Call of Duty: Black Ops - Official Trailer"),
            self._candidate(video_id="bbbBBB22222", title="Hellraiser: Revival - Official Trailer"),
        ]
        with mock.patch("unicornio_editor.trailer._search_youtube", return_value=candidates), mock.patch(
            "unicornio_editor.trailer._fetch_oembed", return_value=self._oembed()
        ):
            result = find_game_trailer("Hellraiser: Revival")
        self.assertEqual(result["video_id"], "bbbBBB22222")

    def test_returns_none_when_no_relevant_trailer(self):
        candidates = [self._candidate(video_id="aaaAAA11111", title="Random Movie - Official Trailer")]
        with mock.patch("unicornio_editor.trailer._search_youtube", return_value=candidates):
            self.assertIsNone(find_game_trailer("Hellraiser: Revival"))

    def test_returns_none_when_oembed_fails(self):
        with mock.patch("unicornio_editor.trailer._search_youtube", return_value=[self._candidate()]), mock.patch(
            "unicornio_editor.trailer._fetch_oembed", return_value=None
        ):
            self.assertIsNone(find_game_trailer("Hellraiser: Revival"))

    def test_returns_none_when_search_fails(self):
        with mock.patch("unicornio_editor.trailer._search_youtube", return_value=[]):
            self.assertIsNone(find_game_trailer("Hellraiser: Revival"))

    def test_audited_discovery_distinguishes_transport_failure(self):
        with mock.patch("unicornio_editor.trailer._search_youtube", return_value=None):
            trailer, status = find_game_trailer_with_status("Hellraiser: Revival")
        self.assertIsNone(trailer)
        self.assertEqual(status, "search_failed")

    def test_rejects_empty_game_name(self):
        with self.assertRaises(TrailerError):
            find_game_trailer("   ")

    def test_build_html_escapes_and_credits(self):
        html = build_trailer_html({
            "video_id": "abcDEF12345",
            "title": 'Hellraiser <b>Trailer</b>',
            "author_name": "Boss & Team",
            "author_url": "https://www.youtube.com/@BossTeamGames",
            "watch_url": "https://www.youtube.com/watch?v=abcDEF12345",
        })
        self.assertIn('src="https://www.youtube-nocookie.com/embed/abcDEF12345"', html)
        self.assertIn("Hellraiser &lt;b&gt;Trailer&lt;/b&gt;", html)
        self.assertIn("Boss &amp; Team", html)
        self.assertIn('class="aligncenter"', html)
        self.assertIn('rel="nofollow noopener"', html)

    def test_build_html_rejects_invalid_video_id(self):
        with self.assertRaises(TrailerError):
            build_trailer_html({"video_id": "../../etc"})


if __name__ == "__main__":
    unittest.main()
