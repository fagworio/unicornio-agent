"""Small, dependency-free WordPress REST client."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError as UrlHTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Config


class WordPressError(RuntimeError):
    """Base error for WordPress API failures."""


class SafetyError(WordPressError):
    """Raised when a request would violate an editorial safety invariant."""


class WordPressClient:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = f"{config.wordpress_url}{config.wordpress_api_base}"

    def list_pending(self, *, page: int = 1, per_page: int = 10) -> list[dict[str, Any]]:
        if not 1 <= page <= 10000 or not 1 <= per_page <= 100:
            raise ValueError("page and per_page are outside safe limits")
        data = self._request(
            "GET", "/posts", {"status": "pending", "page": page, "per_page": per_page}
        )
        if not isinstance(data, list):
            raise WordPressError("WordPress returned an invalid posts collection")
        return data

    def get_post(self, post_id: int) -> dict[str, Any]:
        return self._expect_object(self._request("GET", f"/posts/{self._id(post_id)}"))

    def get_media(self, media_id: int) -> dict[str, Any]:
        return self._expect_object(self._request("GET", f"/media/{self._id(media_id)}"))

    def update_post(self, post_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.config.dry_run:
            raise SafetyError("dry-run blocks WordPress writes")
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        if "status" in payload:
            raise SafetyError("status must never be included in an update payload")
        return self._expect_object(
            self._request("POST", f"/posts/{self._id(post_id)}", body=dict(payload))
        )

    def _request(
        self,
        method: str,
        path: str,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {"Accept": "application/json"}
        if self.config.app_user and self.config.app_password:
            token = f"{self.config.app_user}:{self.config.app_password}".encode()
            headers["Authorization"] = f"Basic {base64.b64encode(token).decode()}"
        encoded_body = None
        if body is not None:
            encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=encoded_body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.config.http_timeout) as response:
                raw = response.read()
        except UrlHTTPError as exc:
            detail = exc.read(512).decode("utf-8", errors="replace")
            raise WordPressError(f"WordPress HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise WordPressError(f"WordPress connection failed: {exc.reason}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WordPressError("WordPress returned invalid JSON") from exc

    @staticmethod
    def _id(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("WordPress id must be a positive integer")
        return value

    @staticmethod
    def _expect_object(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise WordPressError("WordPress returned an invalid object")
        return value
