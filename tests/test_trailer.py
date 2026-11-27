import datetime
import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.error import HTTPError

from unicornio_editor.trailer import (
    TrailerError,
    build_trailer_html,
    find_game_trailer,
    find_game_trailer_with_status,
    find_cached_game_trailer_with_status,
    cached_game_trailer_checked_at,
    trailer_search_evidence_is_recent,
    trailer_search_evidence_matches_cache,
    trailer_search_query,
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
    def test_game_name_queries_decode_entities_and_split_semicolon_names(self):
        self.assertEqual(
            trailer_search_query("Pokémon &amp; Bros.; Hades II"),
            "Pokémon & Bros. trailer; Hades II trailer",
        )
        with mock.patch("unicornio_editor.trailer._search_youtube", return_value=[]) as search:
            find_game_trailer_with_status("Pokémon &amp; Bros.")
        self.assertEqual(search.call_args.args[0], "Pokémon & Bros. trailer")

    def test_absence_evidence_must_be_recent_and_match_provider_and_query(self):
        now = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=datetime.timezone.utc)
        evidence = {
            "query": "Game One trailer; Game Two trailer",
            "provider": "youtube",
            "searched_at": (now - datetime.timedelta(minutes=5)).isoformat(),
            "result": "official_not_found",
        }
        self.assertTrue(
            trailer_search_evidence_is_recent(evidence, "Game One; Game Two", now=now)
        )
        stale = {
            **evidence,
            "searched_at": (now - datetime.timedelta(hours=6, seconds=1)).isoformat(),
        }
        self.assertFalse(
            trailer_search_evidence_is_recent(stale, "Game One; Game Two", now=now)
        )
        wrong_provider = {**evidence, "provider": "fixture"}
        self.assertFalse(
            trailer_search_evidence_is_recent(wrong_provider, "Game One; Game Two", now=now)
        )
        wrong_query = {**evidence, "query": "Other Game trailer"}
        self.assertFalse(
            trailer_search_evidence_is_recent(wrong_query, "Game One; Game Two", now=now)
        )

    def test_negative_search_evidence_uses_original_cache_time(self):
        discover = mock.Mock(return_value=(None, "official_not_found"))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            find_cached_game_trailer_with_status(
                "Game One", root=root, discover=discover
            )
            checked_at = cached_game_trailer_checked_at(root, "Game One")
            self.assertIsNotNone(checked_at)
            self.assertTrue(
                trailer_search_evidence_is_recent(
                    {
                        "query": trailer_search_query("Game One"),
                        "provider": "youtube",
                        "searched_at": checked_at,
                        "result": "official_not_found",
                    },
                    "Game One",
                )
            )

    def test_verified_trailer_is_reused_from_persistent_cache(self):
        trailer = {"video_id": "abcDEF12345", "title": "Official Trailer"}
        discover = mock.Mock(return_value=(trailer, "found"))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = find_cached_game_trailer_with_status(
                "Example Game", root=root, discover=discover
            )
            second = find_cached_game_trailer_with_status(
                "EXAMPLE   GAME", root=root, discover=discover
            )
        self.assertEqual(first, (trailer, "found"))
        self.assertEqual(second, (trailer, "found"))
        discover.assert_called_once()

    def test_trailer_cache_does_not_reuse_negative_result_for_reordered_identity(self):
        first_discover = mock.Mock(return_value=(None, "official_not_found"))
        second_discover = mock.Mock(return_value=(None, "search_failed"))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = find_cached_game_trailer_with_status(
                "Alpha Beta Saga", root=root, discover=first_discover
            )
            second = find_cached_game_trailer_with_status(
                "Saga Beta Alpha", root=root, discover=second_discover
            )
        self.assertEqual(first[1], "official_not_found")
        self.assertEqual(second[1], "search_failed")
        second_discover.assert_called_once()

    def test_search_failure_is_never_cached(self):
        discover = mock.Mock(return_value=(None, "search_failed"))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            find_cached_game_trailer_with_status("Example Game", root=root, discover=discover)
            find_cached_game_trailer_with_status("Example Game", root=root, discover=discover)
        self.assertEqual(discover.call_count, 2)

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

    def test_fan_or_misattributed_trailer_is_not_accepted_or_cached_as_absence(self):
        fan = self._candidate(title="Hellraiser Revival Fan Trailer")
        with mock.patch("unicornio_editor.trailer._search_youtube", return_value=[fan]), mock.patch(
            "unicornio_editor.trailer._fetch_oembed", return_value=self._oembed()
        ):
            trailer, status = find_game_trailer_with_status("Hellraiser Revival")
        self.assertIsNone(trailer)
        self.assertEqual(status, "unverified_candidates")

    def test_search_interstitial_is_not_cached_as_official_absence(self):
        with mock.patch(
            "unicornio_editor.trailer.urlopen",
            return_value=mock.MagicMock(__enter__=lambda self: self, __exit__=lambda *_a: None,
                                        read=lambda _n: b"captcha unusual traffic"),
        ):
            self.assertIsNone(__import__("unicornio_editor.trailer", fromlist=["_search_youtube"])._search_youtube(
                "example trailer", limit=5, timeout=1
            ))

    def _search_page(self, contents):
        data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {
            "sectionListRenderer": {"contents": contents},
        }}}}
        return f"<script>var ytInitialData = {json.dumps(data)};</script>".encode()

    def test_malformed_or_unrecognized_search_data_is_not_proof_of_absence(self):
        for body in (
            b"<script>var ytInitialData = {broken};</script>",
            b'<script>var ytInitialData = {"responseContext": {}};</script>',
            self._search_page(["bad section"]),
            self._search_page([{"itemSectionRenderer": {"contents": {"invalid": "shape"}}}]),
            self._search_page([{"itemSectionRenderer": {"contents": [{"newRenderer": {}}]}}]),
        ):
            with self.subTest(body=body), mock.patch(
                "unicornio_editor.trailer.urlopen", return_value=BytesIO(body)
            ):
                self.assertEqual(find_game_trailer_with_status("Hellraiser: Revival"), (None, "search_failed"))

    def test_mixed_unrecognized_result_never_certifies_absence_or_hides_verified_match(self):
        def page(title):
            known = {"videoRenderer": {
                "videoId": "abcDEF12345", "title": {"simpleText": title},
                "ownerText": {"simpleText": "Boss Team Games"},
            }}
            return self._search_page([
                {"itemSectionRenderer": {"contents": [known, {"newResultRenderer": {}}]}},
                {"continuationItemRenderer": {}},
            ])

        with mock.patch(
            "unicornio_editor.trailer.urlopen",
            return_value=BytesIO(page("Entirely Different Movie - Official Trailer")),
        ), mock.patch("unicornio_editor.trailer._fetch_oembed") as oembed:
            # The unknown renderer can hide a result even when the one known
            # result fills the explicitly bounded candidate window.
            self.assertEqual(
                find_game_trailer_with_status("Hellraiser: Revival", max_candidates=1),
                (None, "unverified_candidates"),
            )
            oembed.assert_not_called()

        with mock.patch(
            "unicornio_editor.trailer.urlopen",
            return_value=BytesIO(page("Hellraiser: Revival - Official Trailer")),
        ), mock.patch("unicornio_editor.trailer._fetch_oembed", return_value=self._oembed()):
            value, status = find_game_trailer_with_status("Hellraiser: Revival", max_candidates=1)
        self.assertEqual(status, "found")
        self.assertEqual(value["video_id"], "abcDEF12345")

    def test_structured_empty_search_is_proof_of_absence(self):
        for sections in ([], [{"itemSectionRenderer": {"contents": []}}]):
            with self.subTest(sections=sections), mock.patch(
                "unicornio_editor.trailer.urlopen", return_value=BytesIO(self._search_page(sections))
            ):
                self.assertEqual(find_game_trailer_with_status("Hellraiser: Revival"), (None, "official_not_found"))

    def test_fallback_video_ids_are_unverified_and_not_cached(self):
        with TemporaryDirectory() as directory, mock.patch(
            "unicornio_editor.trailer.urlopen",
            side_effect=lambda *_args, **_kwargs: BytesIO(b'<div>{"videoId": "abcDEF12345"}</div>'),
        ) as transport:
            kwargs = {"root": Path(directory)}
            for _ in range(2):
                self.assertEqual(
                    find_cached_game_trailer_with_status("Hellraiser: Revival", **kwargs),
                    (None, "unverified_candidates"),
                )
            self.assertIsNone(cached_game_trailer_checked_at(Path(directory), "Hellraiser: Revival"))
        self.assertEqual(transport.call_count, 2, "one search per call; no oEmbed without trustworthy channel")

    def test_search_transport_and_interstitial_are_failures(self):
        for error in (TimeoutError(), HTTPError("https://www.youtube.com/results", 429, "", {}, None)):
            with self.subTest(error=error), mock.patch(
                "unicornio_editor.trailer.urlopen", side_effect=error
            ):
                self.assertEqual(find_game_trailer_with_status("Hellraiser: Revival"), (None, "search_failed"))
        for body in (b"Before you continue to YouTube", b"captcha"):
            with self.subTest(body=body), mock.patch(
                "unicornio_editor.trailer.urlopen", return_value=BytesIO(body)
            ):
                self.assertEqual(find_game_trailer_with_status("Hellraiser: Revival"), (None, "search_failed"))

    def test_matching_candidates_with_missing_or_inconsistent_metadata_are_not_absence(self):
        correct = self._oembed()
        for meta in (
            None, {}, {**correct, "title": "Entirely Different Game - Official Trailer"},
            {**correct, "title": "Hellraiser: Revival - Fan Trailer"},
            {**correct, "author_name": "Other channel"},
        ):
            with self.subTest(meta=meta), mock.patch(
                "unicornio_editor.trailer._search_youtube", return_value=[self._candidate()]
            ), mock.patch("unicornio_editor.trailer._fetch_oembed", return_value=meta):
                self.assertEqual(
                    find_game_trailer_with_status("Hellraiser: Revival"), (None, "unverified_candidates")
                )

    def test_expired_or_legacy_negative_never_produces_current_waiver(self):
        game = "Hellraiser: Revival"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            discover = mock.Mock(return_value=(None, "official_not_found"))
            find_cached_game_trailer_with_status(game, root=root, discover=discover)
            find_cached_game_trailer_with_status(game, root=root, discover=discover)
            discover.assert_called_once()
            path = root / "work/trailer_cache.json"
            cache = json.loads(path.read_text())
            key, current = next(iter(cache.items()))
            proof = {
                "query": trailer_search_query(game), "provider": "youtube",
                "searched_at": current["checked_at"], "result": "official_not_found",
            }
            self.assertTrue(trailer_search_evidence_matches_cache(proof, game, root))
            legacy = {k: current[k] for k in ("query", "provider", "status", "checked_at", "trailer")}
            path.write_text(json.dumps({key: legacy}))
            self.assertIsNone(cached_game_trailer_checked_at(root, game))
            self.assertFalse(trailer_search_evidence_matches_cache(proof, game, root))
            unavailable = mock.Mock(return_value=(None, "search_failed"))
            self.assertEqual(find_cached_game_trailer_with_status(game, root=root, discover=unavailable)[1], "search_failed")
            unavailable.assert_called_once()
            expired = {**current, "checked_at": "2000-01-01T00:00:00+00:00"}
            path.write_text(json.dumps({key: expired}))
            self.assertIsNone(cached_game_trailer_checked_at(root, game))
            self.assertFalse(trailer_search_evidence_matches_cache({**proof, "searched_at": expired["checked_at"]}, game, root))
            find_cached_game_trailer_with_status(game, root=root, discover=unavailable)
            self.assertEqual(unavailable.call_count, 2)

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
