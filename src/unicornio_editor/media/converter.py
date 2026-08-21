"""Image verification, WebP conversion and featured-size preparation."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

FEATURED_WIDTH = 1200
FEATURED_HEIGHT = 720


class MediaConversionError(RuntimeError):
    """Raised when an image cannot be verified or converted."""


def _open_authoritative(source: Path) -> Image.Image:
    """Open an image with its EXIF orientation applied.

    Camera/phone photos store the rotation in EXIF (e.g. Orientation=6) with
    the raw pixels lying sideways; PIL does NOT apply it automatically, so a
    naive open would convert and publish the photo lying down. Every pipeline
    open must go through this helper.
    """
    with Image.open(source) as image:
        image.verify()
    image = Image.open(source)
    return ImageOps.exif_transpose(image)


def convert_to_webp(source: Path, destination: Path | None = None) -> Path:
    source = Path(source)
    destination = destination or source.with_suffix(".webp")
    try:
        image = _open_authoritative(source)
        with image:
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

    The EXIF orientation is applied first (a photo stored sideways would
    otherwise be published lying down) and PORTRAIT sources are rejected:
    cover-cropping a portrait into 5:3 destroys more than half the frame, so
    a portrait featured image is a wrong image — the agent must pick a
    landscape key art instead (fail-closed policy).
    """
    source = Path(source)
    destination = destination or source.with_name(source.stem + "_featured.webp")
    try:
        image = _open_authoritative(source)
        with image:
            if image.width < 64 or image.height < 64:
                raise MediaConversionError("image resolution is below 64x64")
            if image.width < image.height:
                raise MediaConversionError(
                    f"featured source is portrait ({image.width}x{image.height} after EXIF transpose); "
                    "a 1200x720 featured image requires a landscape source — pick landscape key art"
                )
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
