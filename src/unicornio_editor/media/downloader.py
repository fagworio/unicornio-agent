"""Bounded image downloads with MIME and size checks."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class MediaDownloadError(RuntimeError):
    """Raised when a remote image cannot be safely downloaded."""


def download_image(url: str, destination: Path, *, max_bytes: int = 8 * 1024 * 1024) -> Path:
    if not url.startswith(("http://", "https://")):
        raise MediaDownloadError("image URL must use HTTP(S)")
    if max_bytes < 1024:
        raise MediaDownloadError("max_bytes is too small")
    request = Request(url, headers={"Accept": "image/*", "User-Agent": "unicornio-editor/0.1"})
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
    except (HTTPError, URLError, OSError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc, MediaDownloadError):
            raise
        raise MediaDownloadError("image download failed") from exc
    if written == 0:
        destination.unlink(missing_ok=True)
        raise MediaDownloadError("remote image was empty")
    return destination
