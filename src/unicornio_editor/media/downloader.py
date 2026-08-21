"""Bounded image downloads with MIME, size checks and rate-limit retries."""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class MediaDownloadError(RuntimeError):
    """Raised when a remote image cannot be safely downloaded."""


LEGACY_LOCAL_UPLOAD_PATH = "/wp-content/uploads/2019/06/"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 6


def _retry_delay(attempt: int, response_headers=None) -> float:
    """Backoff that honors the server's Retry-After signal (e.g. Wikimedia 429s)."""
    base = 2.0 * attempt
    if response_headers is not None:
        try:
            retry_after = float(response_headers.get("Retry-After", ""))
            return max(base, retry_after + 1.0)
        except (TypeError, ValueError):
            pass
    return base


def select_reupload_source(local_url: str, effective_url: str | None = None) -> str:
    """Choose a safe source when a legacy local upload needs re-importing."""
    parsed = urlparse(local_url)
    if parsed.path.startswith(LEGACY_LOCAL_UPLOAD_PATH):
        if not effective_url or not effective_url.startswith(("http://", "https://")):
            raise MediaDownloadError("legacy local upload requires an effective source URL")
        return effective_url
    return local_url


def download_image(url: str, destination: Path, *, max_bytes: int = 8 * 1024 * 1024) -> Path:
    if not url.startswith(("http://", "https://")):
        raise MediaDownloadError("image URL must use HTTP(S)")
    if max_bytes < 1024:
        raise MediaDownloadError("max_bytes is too small")
    request = Request(url, headers={"Accept": "image/*", "User-Agent": "unicornio-editor/0.1"})
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=30) as response:
                content_type = response.headers.get_content_type()
                if not content_type.startswith("image/"):
                    raise MediaDownloadError("remote resource is not an image")
                declared_length = response.headers.get("Content-Length")
                if declared_length and int(declared_length) > max_bytes:
                    raise MediaDownloadError("remote image exceeds size limit")
                destination = Path(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with destination.open("wb") as output:
                    while True:
                        chunk = response.read(min(64 * 1024, max_bytes - written + 1))
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise MediaDownloadError("remote image exceeds size limit")
                        output.write(chunk)
        except HTTPError as exc:
            destination.unlink(missing_ok=True)
            if exc.code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                time.sleep(_retry_delay(attempt, exc.headers))
                continue
            raise MediaDownloadError(f"image download failed (HTTP {exc.code})") from exc
        except (URLError, OSError, ValueError) as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, MediaDownloadError):
                raise
            last_error = exc
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_retry_delay(attempt))
                continue
            raise MediaDownloadError("image download failed") from exc
        if written == 0:
            destination.unlink(missing_ok=True)
            raise MediaDownloadError("remote image was empty")
        return destination
    raise MediaDownloadError("image download failed") from last_error
