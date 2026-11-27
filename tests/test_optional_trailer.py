from unittest.mock import Mock

import pytest

from unicornio_editor import trailer
from unicornio_editor.config import Config
from unicornio_editor.workflow import compose_final_content

VIDEO = "abcDEF12345"
OTHER = "xyzXYZ54321"
GAME = "Hollow Knight Silksong"
TITLE = f"{GAME} - Official Trailer"
CHANNEL = "Team Cherry"
HTML = "<p>Novidades do jogo.</p>"


def listing(video=VIDEO, *, title=TITLE, channel=CHANNEL):
    return {"video_id": video, "title": title, "channel": channel}


def metadata(**overrides):
    return {
        "title": TITLE,
        "author_name": CHANNEL,
        "author_url": "https://www.youtube.com/@TeamCherry",
        "thumbnail_url": "https://i.ytimg.com/unneeded.jpg",
        **overrides,
    }


def compose(url, *, html=HTML):
    return compose_final_content(
        {"game_name": GAME, "trailer_url": url, "cleaned_html": html},
        Config(content_source="wordpress", wordpress_url="https://wp.test", wordpress_api_base="/wp-json/wp/v2", internal_links_enabled=False), None,
    )


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={VIDEO}&list=PL42",
    f"https://youtu.be/{VIDEO}?t=5",
    f"https://www.youtube-nocookie.com/embed/{VIDEO}",
])
def test_proposed_id_checked_before_search_and_embedded_once(monkeypatch, url):
    search = Mock(return_value=[listing(OTHER), listing()])
    lookup = Mock(return_value=metadata())
    monkeypatch.setattr(trailer, "_search_youtube", search)
    monkeypatch.setattr(trailer, "_fetch_oembed", lookup)

    html, selected, status = compose(url)
    assert status == "found" and selected["video_id"] == VIDEO
    search.assert_called_once()
    assert search.call_args.args == (VIDEO,)
    lookup.assert_called_once()
    assert lookup.call_args.args == (VIDEO,)
    assert '<figure class="aligncenter">' in html
    assert f'src="https://www.youtube-nocookie.com/embed/{VIDEO}"' in html
    assert f'https://www.youtube.com/watch?v={VIDEO}' in html
    assert "<figcaption>Trailer:" in html and TITLE in html and CHANNEL in html
    assert "<img" not in html and "unneeded.jpg" not in html
    repeated, _, _ = compose(url, html=HTML + trailer.build_trailer_html(selected))
    assert repeated.count("<iframe") == 1


@pytest.mark.parametrize("url", [
    f"http://youtube.com/watch?v={VIDEO}",
    f"https://youtube.com.evil.example/watch?v={VIDEO}",
    f"https://evil.example/?youtube.com/watch?v={VIDEO}",
    f"https://bad@youtube.com/watch?v={VIDEO}",
    f"https://youtube.com:444/watch?v={VIDEO}",
    f"https://youtube.com/watch?v={VIDEO}&v={OTHER}",
    "https://youtube.com/watch?v=short",
])
def test_invalid_proposal_goes_straight_to_game_search(monkeypatch, url):
    search = Mock(return_value=[listing(OTHER)])
    monkeypatch.setattr(trailer, "_search_youtube", search)
    monkeypatch.setattr(trailer, "_fetch_oembed", Mock(return_value=metadata()))

    html, selected, status = compose(url)
    assert status == "found" and selected["video_id"] == OTHER
    assert VIDEO not in html
    search.assert_called_once()
    assert search.call_args.args == (f"{GAME} trailer",)


@pytest.mark.parametrize(("candidate", "meta"), [
    (listing(OTHER), metadata()),
    (listing(title=f"{GAME} Fan Trailer"), metadata()),
    (listing(channel="A different channel"), metadata()),
    (listing(), metadata(title="Another Game Official Trailer")),
    (listing(), metadata(author_url="https://evil.example/@TeamCherry")),
    (listing(), None),
])
def test_unverified_exact_proposal_is_not_embedded(monkeypatch, candidate, meta):
    search = Mock(side_effect=[[candidate], None])
    monkeypatch.setattr(trailer, "_search_youtube", search)
    monkeypatch.setattr(trailer, "_fetch_oembed", Mock(return_value=meta))
    html, selected, status = compose(f"https://youtu.be/{VIDEO}")
    assert selected is None and status == "search_failed"
    assert "<iframe" not in html and "<img" not in html
    assert [call.args[0] for call in search.call_args_list] == [VIDEO, f"{GAME} trailer"]


def test_fake_or_commented_iframe_is_not_a_matching_embed():
    valid = f'<iframe src="https://www.youtube.com/embed/{VIDEO}"></iframe>'
    assert trailer.has_youtube_embed(valid, video_id=VIDEO)
    assert not trailer.has_youtube_embed(valid, video_id=OTHER)
    assert not trailer.has_youtube_embed(f"<!-- {valid} -->")
    assert not trailer.has_youtube_embed(
        f'<iframe data-src="https://www.youtube.com/embed/{VIDEO}" src="https://evil.example"></iframe>'
    )
