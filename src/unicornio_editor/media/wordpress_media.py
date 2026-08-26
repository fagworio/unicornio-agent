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
    # Captions/créditos vão ao WordPress como TEXT (HTML cru renderizaria tags
    # quebradas no caption da Media Library / featured). Sanitiza para texto
    # puro; o alt tambem nunca deve carregar markup.
    from .text import plain_text

    credit = plain_text(evidence["credit_text"])
    alt = plain_text(evidence["alt_text"])
    return client.upload_media(
        path,
        filename=path.name,
        alt_text=alt,
        title=credit,
        caption=credit,
    )
