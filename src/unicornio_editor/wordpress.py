"""Small, dependency-free WordPress REST client."""

from __future__ import annotations

import base64
import json
import mimetypes
import uuid
from collections.abc import Mapping
from pathlib import Path
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

    def list_pending(
        self, *, page: int = 1, per_page: int = 10, include: list[int] | None = None
    ) -> list[dict[str, Any]]:
        if not 1 <= page <= 10000 or not 1 <= per_page <= 100:
            raise ValueError("page and per_page are outside safe limits")
        # Query `status=pending` server-side: some installations (including
        # production) hide non-published statuses from the unfiltered listing,
        # so a local filter can never discover pending posts. The local filter
        # below remains as defense in depth.
        query: dict[str, Any] = {
            "context": "edit",
            "status": "pending",
            "page": page,
            "per_page": per_page,
        }
        if include:
            query["include"] = ",".join(str(int(pid)) for pid in include)
        try:
            data = self._request("GET", "/posts", query)
        except WordPressError as exc:
            # Some installations reject even a read query containing `status`
            # (HTTP 4xx). Fall back to an unfiltered page and filter locally.
            if not str(exc).startswith("WordPress HTTP"):
                raise
            data = self._request(
                "GET", "/posts", {"context": "edit", "page": page, "per_page": per_page}
            )
        if not isinstance(data, list):
            raise WordPressError("WordPress returned an invalid posts collection")
        return [post for post in data if isinstance(post, dict) and post.get("status") == "pending"]

    def get_post(self, post_id: int) -> dict[str, Any]:
        # context=edit e obrigatorio: sem ele a REST nao expoe content.raw,
        # title.raw nem os custom fields (meta) que o pipeline consome.
        return self._expect_object(
            self._request("GET", f"/posts/{self._id(post_id)}", {"context": "edit"})
        )

    def get_media(self, media_id: int) -> dict[str, Any]:
        return self._expect_object(self._request("GET", f"/media/{self._id(media_id)}"))

    def search_media(self, query: str, *, per_page: int = 10) -> list[dict[str, Any]]:
        """Search the local Media Library by title/alt/caption/description.

        Reuse candidates for the editorial pipeline: images already uploaded
        by previous runs carry their full credit block in the attachment
        title/caption, which is the license evidence required for reuse. The
        caller must NEVER edit the original attachment — a reused image is
        downloaded and re-uploaded as a NEW attachment.
        """
        query = str(query or "").strip()
        if not query:
            raise ValueError("search query must not be empty")
        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be in [1, 100]")
        data = self._request(
            "GET",
            "/media",
            {"search": query, "per_page": per_page, "orderby": "date", "order": "desc"},
        )
        if not isinstance(data, list):
            raise WordPressError("WordPress returned an invalid media collection")
        return [item for item in data if isinstance(item, dict)]

    def publish(self, post_id: int, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deliberately publish a post (status=publish).

        This is an explicit, separate operation on purpose: ``update_post``
        refuses to touch ``status`` for pipeline safety. Callers MUST gate
        this behind the pre-publish checklist — the method itself performs
        no safety checks by design.
        """
        if self.config.dry_run:
            raise SafetyError("dry-run blocks WordPress writes")
        payload: dict[str, Any] = {"status": "publish"}
        if meta:
            payload["meta"] = meta
        return self._expect_object(self._request("POST", f"/posts/{self._id(post_id)}", body=payload))

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

    def upload_media(
        self,
        path: str,
        *,
        filename: str,
        alt_text: str,
        title: str,
        caption: str | None = None,
    ) -> dict[str, Any]:
        if self.config.dry_run:
            raise SafetyError("dry-run blocks WordPress media uploads")
        media_path = Path(path)
        if not media_path.is_file() or media_path.suffix.lower() != ".webp":
            raise SafetyError("media upload requires an existing WebP file")
        content = media_path.read_bytes()
        if len(content) > 8 * 1024 * 1024:
            raise SafetyError("media upload exceeds the 8 MiB limit")
        boundary = uuid.uuid4().hex.encode("ascii")
        chunks: list[bytes] = []

        def field(name: str, value: str) -> None:
            chunks.extend(
                [
                    b"--" + boundary + b"\r\n",
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )

        field("alt_text", alt_text)
        field("title", title)
        if caption:
            field("caption", caption)
        mime = mimetypes.guess_type(filename)[0] or "image/webp"
        chunks.extend(
            [
                b"--" + boundary + b"\r\n",
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {mime}\r\n\r\n".encode(),
                content,
                b"\r\n--" + boundary + b"--\r\n",
            ]
        )
        request = Request(
            f"{self.base_url}/media",
            data=b"".join(chunks),
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
                **self._auth_header(),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.http_timeout) as response:
                value = json.loads(response.read())
        except (UrlHTTPError, URLError, json.JSONDecodeError) as exc:
            raise WordPressError("WordPress media upload failed") from exc
        return self._expect_object(value)

    def _auth_header(self) -> dict[str, str]:
        if not self.config.app_user or not self.config.app_password:
            return {}
        token = f"{self.config.app_user}:{self.config.app_password}".encode()
        return {"Authorization": f"Basic {base64.b64encode(token).decode()}"}

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
