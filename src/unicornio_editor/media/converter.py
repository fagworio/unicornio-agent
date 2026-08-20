"""Image verification and WebP conversion."""

from __future__ import annotations

from pathlib import Path


class MediaConversionError(RuntimeError):
    """Raised when an image cannot be verified or converted."""


def convert_to_webp(source: Path, destination: Path | None = None) -> Path:
    source = Path(source)
    destination = destination or source.with_suffix(".webp")
    try:
        from PIL import Image

        with Image.open(source) as image:
            image.verify()
        with Image.open(source) as image:
            if image.width < 64 or image.height < 64:
                raise MediaConversionError("image resolution is below 64x64")
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA")
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination, format="WEBP", quality=85, method=6)
    except MediaConversionError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise MediaConversionError("image could not be verified or converted to WebP") from exc
    return destination
