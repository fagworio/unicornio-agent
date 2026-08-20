"""Image verification, WebP conversion and featured-size preparation."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

FEATURED_WIDTH = 1200
FEATURED_HEIGHT = 720


class MediaConversionError(RuntimeError):
    """Raised when an image cannot be verified or converted."""


def convert_to_webp(source: Path, destination: Path | None = None) -> Path:
    source = Path(source)
    destination = destination or source.with_suffix(".webp")
    try:
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
        raise MediaConversionError("image conversion failed") from exc
    return destination


def prepare_featured_webp(source: Path, destination: Path | None = None) -> Path:
    """Center-crop and resize to exactly 1200x720 (5:3) and save as WebP.

    Featured images must always obey the portal's 1200x720 ratio, so the
    source is cover-cropped from its center and resized to the exact target
    (upscaling allowed so the guaranteed size holds for small sources).
    """
    source = Path(source)
    destination = destination or source.with_name(source.stem + "_featured.webp")
    try:
        with Image.open(source) as image:
            image.verify()
        with Image.open(source) as image:
            if image.width < 64 or image.height < 64:
                raise MediaConversionError("image resolution is below 64x64")
            work = image.convert("RGBA") if image.mode not in {"RGB", "RGBA"} else image
            target_ratio = FEATURED_WIDTH / FEATURED_HEIGHT
            width, height = work.size
            current_ratio = width / height
            if current_ratio > target_ratio:
                new_width = int(height * target_ratio)
                left = (width - new_width) // 2
                work = work.crop((left, 0, left + new_width, height))
            elif current_ratio < target_ratio:
                new_height = int(width / target_ratio)
                top = (height - new_height) // 2
                work = work.crop((0, top, width, top + new_height))
            work = work.resize((FEATURED_WIDTH, FEATURED_HEIGHT), Image.Resampling.LANCZOS)
            destination.parent.mkdir(parents=True, exist_ok=True)
            work.save(destination, format="WEBP", quality=85, method=6)
    except MediaConversionError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise MediaConversionError("featured image conversion failed") from exc
    return destination
