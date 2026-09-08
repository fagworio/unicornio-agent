"""Upload verified media through the local WordPress Media Library."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import re
import unicodedata

from .license import validate_candidate


def friendly_media_filename(path: Path, candidate: dict[str, Any]) -> str:
    """Return a human-readable, stable WebP filename for a new attachment.

    Conversion happens in temporary paths such as ``inline_0.webp``. Using
    that temporary name in WordPress leaks an opaque URL into the article and
    Media Library. The editorial alt describes the actual image and is already
    validated, so it is the safest source for a readable filename.
    """
    from .text import plain_text

    label = plain_text(candidate.get("alt_text"))
    normalized = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if len(slug) < 3:
        source_stem = Path(str(candidate.get("direct_image_url") or "").split("?", 1)[0]).stem
        slug = re.sub(r"[^a-z0-9]+", "-", source_stem.lower()).strip("-")
    if len(slug) < 3:
        slug = "imagem-editorial"
    return f"{slug[:90].rstrip('-')}.webp"


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
        filename=friendly_media_filename(path, evidence),
        alt_text=alt,
        title=alt or "Imagem editorial",
        caption=credit,
    )
