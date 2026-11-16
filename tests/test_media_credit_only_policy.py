import pytest

from unicornio_editor.batch import _context_for_post, _media_manifest_candidate
from unicornio_editor.cli import _enriquecer_candidatos
from unicornio_editor.config import Config
from unicornio_editor.editorial_schema import EditorialValidationError, validate_editorial
from unicornio_editor.media.evidence import evidence_score
from unicornio_editor.media import visual_hash
from unicornio_editor.media.wordpress_media import upload_image
from unicornio_editor.workflow import _media_item_rejection


def _editorial(media_item):
    return {
        "site_relevance": {
            "decision": "process",
            "confidence": 0.99,
            "reason": "Notícia de videogame.",
            "matched_topics": ["games"],
        },
        "media_plan": [media_item],
        "needs_trailer": False,
        "trailer_url": None,
        "game_name": "Moonlight Peaks",
    }


def test_prepared_manifest_contract_reflects_effective_media_source_policy(tmp_path):
    post = {
        "id": 42,
        "status": "pending",
        "title": {"raw": "Moonlight Peaks chega ao console"},
        "content": {"raw": "<p>Moonlight Peaks apresenta novas criaturas e exploração.</p>"},
        "featured_media": 0,
        "categories": [],
        "meta": {"original_link": "https://publisher.example/article"},
    }
    prepared = {
        "cleaned_html": "<p>Moonlight Peaks apresenta novas criaturas e exploração.</p>",
        "original_link": "https://publisher.example/article",
    }

    def context(policy):
        return _context_for_post(
            post=post,
            prepared=prepared,
            batch_id="credit-only-policy-test",
            config=Config(
                content_source="wordpress",
                wordpress_url="http://wp.test",
                wordpress_api_base="/wp-json/wp/v2",
                media_source_policy=policy,
            ),
            client=object(),
            root=tmp_path,
        )["requirements"]["media_stage"]

    credit_only = context("credit_only")
    strict = context("strict")
    assert credit_only["media_source_policy"] == "credit_only"
    assert credit_only["source_page_required_for_new_media"] is False
    assert credit_only["credit_required"] is True
    assert credit_only["credit_generation"] == "hosting_domain_not_author"
    assert strict["media_source_policy"] == "strict"
    assert strict["source_page_required_for_new_media"] is True


def test_v1_credit_only_media_plan_needs_direct_url_and_credit_not_source_page():
    item = {
        "paragraph_index": 0,
        "direct_image_url": "https://cdn.example/moonlight-peaks-key-art.webp",
        "credit_text": "Crédito da imagem: CDN Example",
        "alt_text": "Moonlight Peaks - imagem principal",
        "is_featured": True,
    }

    validated = validate_editorial(_editorial(item), media_source_policy="credit_only")

    assert validated["media_plan"][0]["direct_image_url"] == item["direct_image_url"]
    assert validated["media_plan"][0]["credit_text"] == item["credit_text"]
    assert "source_page_url" not in validated["media_plan"][0]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://cdn.example/image.webp",
        "https://",
        "not-a-url",
        "https://cdn.example:bad/image.webp",
        "https://user:pass@cdn.example/image.webp",
    ],
)
def test_credit_only_schema_rejects_invalid_direct_image_url(url):
    item = {
        "paragraph_index": 0,
        "direct_image_url": url,
        "credit_text": "Crédito da imagem: domínio de hospedagem: cdn.example.",
        "alt_text": "Moonlight Peaks - imagem principal",
        "is_featured": True,
        "provenance_mode": "credit_only",
    }
    with pytest.raises(EditorialValidationError, match="direct_image_url"):
        validate_editorial(_editorial(item), media_source_policy="credit_only")


def test_credit_only_discards_invalid_optional_source_page_url():
    item = {
        "paragraph_index": 0,
        "direct_image_url": "https://cdn.example/moonlight-peaks-key-art.webp",
        "source_page_url": "not-a-url",
        "credit_text": "Crédito da imagem: Moonlight Peaks.",
        "alt_text": "Moonlight Peaks - imagem principal",
        "is_featured": True,
    }
    validated = validate_editorial(_editorial(item), media_source_policy="credit_only")
    assert "source_page_url" not in validated["media_plan"][0]
    assert validated["media_plan"][0]["provenance_mode"] == "credit_only"


def test_strict_media_policy_still_requires_source_page():
    item = {
        "paragraph_index": 0,
        "direct_image_url": "https://cdn.example/moonlight-peaks-key-art.webp",
        "credit_text": "Crédito da imagem: CDN Example",
        "alt_text": "Moonlight Peaks - imagem principal",
        "is_featured": True,
    }

    with pytest.raises(EditorialValidationError, match="source_page_url"):
        validate_editorial(_editorial(item), media_source_policy="strict")


def test_credit_only_schema_derives_visible_host_credit_when_omitted():
    item = {
        "paragraph_index": 0,
        "direct_image_url": "https://cdn.example/moonlight-peaks-key-art.webp",
        "alt_text": "Moonlight Peaks - imagem principal",
        "is_featured": True,
    }

    validated = validate_editorial(_editorial(item), media_source_policy="credit_only")

    assert validated["media_plan"][0]["credit_text"] == (
        "Crédito da imagem: domínio de hospedagem: cdn.example."
    )
    assert "autor" not in validated["media_plan"][0]["credit_text"].casefold()
    assert validated["media_plan"][0]["provenance_mode"] == "credit_only"
    assert validated["media_plan"][0]["source_page_verified"] is False
    assert validated["media_plan"][0]["rights_verified"] is False


def test_credit_only_manifest_candidate_projects_url_and_credit_without_fabricating_page():
    item = _media_manifest_candidate(
        {
            "direct_image_url": "https://cdn.example/moonlight-peaks-key-art.webp",
            "verdict": "credit_only_accepted",
        },
        subject="Moonlight Peaks",
        query="Moonlight Peaks official art",
        post_id=115870,
        index=0,
        featured=True,
        media_source_policy="credit_only",
    )

    assert item is not None
    assert item["direct_image_url"].endswith("moonlight-peaks-key-art.webp")
    assert item["credit_text"] == "Crédito da imagem: domínio de hospedagem: cdn.example."
    assert "source_page_url" not in item
    assert item["provenance_mode"] == "credit_only"


def test_credit_only_evidence_can_pass_without_claiming_source_page_verification():
    result = evidence_score(
        "Moonlight Peaks",
        filename="moonlight-peaks-key-art.webp",
        query="Moonlight Peaks official art",
        source_page_present=False,
        allow_credit_only=True,
        credit_text="Crédito da imagem: CDN Example",
    )

    assert result["verdict"] == "credit_only_accepted"
    assert result["gate"] == "credit_only_policy"
    assert result["source_page_verified"] is False
    assert result["needs_vision"] is False


def test_credit_only_evidence_does_not_treat_search_query_as_image_relevance():
    result = evidence_score(
        "Moonlight Peaks",
        filename="valorant-logo.webp",
        query="Moonlight Peaks official art",
        source_page_present=False,
        allow_credit_only=True,
        credit_text="Crédito da imagem: domínio de hospedagem: cdn.example.",
    )

    assert result["verdict"] == "reject"
    assert result["reason_code"] == "credit_only_subject_mismatch"
    assert result["source_page_verified"] is False
    assert result["rights_verified"] is False


def test_credit_only_evidence_without_credit_stays_blocked():
    result = evidence_score(
        "Moonlight Peaks",
        filename="moonlight-peaks-key-art.webp",
        query="Moonlight Peaks official art",
        source_page_present=False,
        allow_credit_only=True,
        credit_text="",
    )

    assert result["verdict"] == "unresolved_source"
    assert result["needs_vision"] is False


def test_media_resolver_rejects_malformed_direct_url_even_with_credit():
    accepted, rejected, _deferred = _enriquecer_candidatos(
        [{
            "candidate_id": "bad-url",
            "engine": "controlled",
            "query": "Moonlight Peaks",
            "direct_image_url": "https://",
            "credit_text": "Crédito da imagem: Moonlight Peaks.",
        }],
        subject="Moonlight Peaks",
        termo="Moonlight Peaks",
        media_source_policy="credit_only",
    )
    assert not accepted
    assert rejected


def test_media_resolver_accepts_url_and_host_credit_without_page(monkeypatch):
    monkeypatch.setattr(
        visual_hash,
        "image_hashes",
        lambda urls, **kwargs: {str(url): "0000000000000000" for url in urls},
    )
    accepted, rejected, deferred = _enriquecer_candidatos(
        [{
            "candidate_id": "candidate-1",
            "engine": "yandex",
            "query": "Moonlight Peaks",
            "direct_image_url": "https://cdn.example/moonlight-peaks-key-art.jpg",
        }],
        subject="Moonlight Peaks",
        termo="Moonlight Peaks",
        verify=True,
        media_source_policy="credit_only",
    )

    assert not rejected
    assert not deferred
    assert len(accepted) == 1
    candidate = accepted[0]
    assert candidate["evidence"]["verdict"] == "credit_only_accepted"
    assert candidate["source_page_verified"] is False
    assert candidate["provenance_mode"] == "credit_only"
    assert candidate["credit_text"] == "Crédito da imagem: domínio de hospedagem: cdn.example."
    assert "source_page_url" not in candidate


def test_credit_only_preflight_rejects_irrelevant_asset_even_when_search_query_matches():
    item = {
        "direct_image_url": "https://cdn.example/valorant-logo.webp",
        "credit_text": "Crédito da imagem: domínio de hospedagem: cdn.example.",
        "alt_text": "",
        "search_query": "Moonlight Peaks official art",
        "subject": "Moonlight Peaks",
        "is_featured": False,
        "provenance_mode": "credit_only",
    }

    reason = _media_item_rejection(
        item,
        {"moonlight peaks"},
        client=object(),
        attachment_cache={},
        editorial={"game_name": "Moonlight Peaks"},
    )

    assert reason is not None
    assert "relacao com o conteudo" in reason


@pytest.mark.parametrize("is_featured", [False, True])
def test_v1_media_preflight_accepts_direct_url_and_credit(is_featured):
    item = {
        "direct_image_url": "https://cdn.example/moonlight-peaks-art.webp",
        "credit_text": "Crédito da imagem: CDN Example",
        "alt_text": "Moonlight Peaks - imagem de apoio",
        "search_query": "Moonlight Peaks official art",
        "subject": "Moonlight Peaks",
        "is_featured": is_featured,
        "provenance_mode": "credit_only",
    }

    assert _media_item_rejection(
        item,
        {"moonlight peaks"},
        client=object(),
        attachment_cache={},
        editorial={"game_name": "Moonlight Peaks"},
    ) is None


def test_wordpress_upload_accepts_credit_only_and_keeps_credit(tmp_path):
    image = tmp_path / "moonlight-peaks.webp"
    image.write_bytes(b"RIFF....WEBP")

    class UploadClient:
        def upload_media(self, path, **kwargs):
            return {"id": 77, "caption": kwargs["caption"], "alt_text": kwargs["alt_text"]}

    result = upload_image(
        UploadClient(),
        image,
        {
            "direct_image_url": "https://cdn.example/moonlight-peaks-art.webp",
            "credit_text": "Crédito da imagem: CDN Example",
            "alt_text": "Moonlight Peaks - imagem principal",
            "provenance_mode": "credit_only",
        },
    )

    assert result["caption"] == "Crédito da imagem: CDN Example"
    assert result["alt_text"] == "Moonlight Peaks - imagem principal"


def test_credit_only_upload_failure_propagates_without_success_result(tmp_path):
    image = tmp_path / "moonlight-peaks.webp"
    image.write_bytes(b"RIFF....WEBP")

    class FailingUploadClient:
        def upload_media(self, _path, **_kwargs):
            raise RuntimeError("isolated upload failure")

    with pytest.raises(RuntimeError, match="isolated upload failure"):
        upload_image(
            FailingUploadClient(),
            image,
            {
                "direct_image_url": "https://cdn.example/moonlight-peaks-art.webp",
                "credit_text": "Crédito da imagem: domínio de hospedagem: cdn.example.",
                "alt_text": "Moonlight Peaks - imagem principal",
                "provenance_mode": "credit_only",
            },
        )


def test_wordpress_upload_rejects_credit_only_without_credit(tmp_path):
    image = tmp_path / "moonlight-peaks.webp"
    image.write_bytes(b"RIFF....WEBP")

    class UploadClient:
        def upload_media(self, *args, **kwargs):
            raise AssertionError("upload should not be reached")

    with pytest.raises(ValueError, match="credit_text"):
        upload_image(
            UploadClient(),
            image,
            {
                "direct_image_url": "https://cdn.example/moonlight-peaks-art.webp",
                "alt_text": "Moonlight Peaks - imagem principal",
                "provenance_mode": "credit_only",
            },
        )
