"""Upload verified media through the local WordPress Media Library."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .license import validate_candidate


def upload_image(client: Any, path: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    evidence = validate_candidate(candidate)
    path = Path(path)
    if path.suffix.lower() != ".webp":
        raise ValueError("only converted WebP files may be uploaded")
    if not path.is_file():
        raise FileNotFoundError(path)
    return client.upload_media(
        path,
        filename=path.name,
        alt_text=evidence["alt_text"],
        title=evidence["credit_text"],
        caption=evidence["credit_text"],
    )
