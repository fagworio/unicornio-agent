"""Image verification, WebP conversion and featured-size preparation."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

FEATURED_WIDTH = 1280
FEATURED_HEIGHT = 720
MAX_INLINE_WIDTH = 1280
# Content images below this width render tiny on the portal layout; the
# source is rejected instead of upscaled (upscaling never happens).
MIN_INLINE_WIDTH = 640


def image_dimensions(path: Path) -> tuple[int, int]:
    """Return (width, height) of an image file without decoding pixels."""
    with Image.open(path) as image:
        return image.size


def image_is_mostly_flat(path, *, sample_px=12000, edge_threshold=18.0) -> bool:
    """Heuristic: a mostly-flat image (no real artwork, only text/banner).

    An image that is essentially a solid/very-low-detail graphic (a text
    banner, a logo on a plain background, a coming soon card) carries no
    editorial artwork. We detect it deterministically by downsampling to a
    small grid and measuring the mean edge magnitude (Sobel-ish via neighbor
    deltas). Below edge_threshold it is treated as mostly flat and the
    featured candidate should be rejected (a text-only featured is not key art).

    Returns True when the image has almost no structural detail.
    """
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("L")
            ratio = max(1.0, (image.width * image.height) / float(sample_px))
            small = image.resize(
                (max(1, int(image.width / ratio ** 0.5)),
                 max(1, int(image.height / ratio ** 0.5))),
                Image.Resampling.BOX,
            )
            w, h = small.size
            pixels = list(small.getdata())
            total = 0.0
            count = 0
            for y in range(1, h - 1):
                row = y * w
                for x in range(1, w - 1):
                    c = pixels[row + x]
                    dx = abs(pixels[row + x + 1] - c) + abs(pixels[row + x - 1] - c)
                    dy = abs(pixels[row + w + x] - c) + abs(pixels[row - w + x] - c)
                    total += dx + dy
                    count += 1
            if count == 0:
                return True
            return (total / count) < edge_threshold
    except Exception:
        return False


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


# Transparent images are rejected: a source whose usable (non-transparent)
# pixels cover less than this fraction of the frame is a defective/empty
# image (logos cut to 3% opacity, blank overlays) — flattening it over white
# would publish an empty frame, so it fails instead of being published.
_MIN_OPAQUE_FRACTION = 0.01


def _alpha_channel(image: Image.Image) -> Image.Image | None:
    """Canal alpha da imagem, ou None quando o modo nao tem transparencia."""
    if image.mode == "RGBA":
        return image.getchannel("A")
    if image.mode in ("LA", "PA"):
        return image.getchannel("A")
    if image.mode == "P" and "transparency" in image.info:
        return image.convert("RGBA").getchannel("A")
    return None


def image_has_transparency(path: Path) -> bool:
    """True quando a imagem tem canal alpha com pelo menos um pixel translucido.

    Read-only, usada pelo pipeline para reportar (e pelo flatten para decidir).
    Modos sem canal alpha (JPEG/RGB) nunca sao transparentes.
    """
    try:
        with Image.open(path) as image:
            alpha = _alpha_channel(image)
            if alpha is None:
                return False
            return alpha.getextrema() != (255, 255)
    except OSError:
        return False


def flatten_transparency(image: Image.Image) -> Image.Image:
    """Compoe a imagem sobre fundo branco e devolve RGB (sem canal alpha).

    A regra editorial e \"imagem transparente nao entra no post\": a fonte
    pode chegar como PNG/WebP com canal alpha (key art recortada, logo), mas o
    arquivo publicado deve ser opaco — fundo transparente vira caixa
    preta/branca dependendo do tema e degrada o layout. Em vez de rejeitar
    toda key art com alpha, o alpha e achatado sobre branco ANTES do upload e
    da insercao no content (a imagem usada nunca e transparente).
    """
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        return background
    if image.mode in ("LA", "PA"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image.convert("RGBA"), mask=image.getchannel("A"))
        return background
    if image.mode == "P" and "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _reject_if_empty_alpha(image: Image.Image) -> None:
    """Fail-closed: imagem (quase) totalmente transparente e rejeitada.

    Flatten nao salva uma imagem vazia — composicao sobre branco so faz
    sentido quando ha conteudo opaco real para publicar.
    """
    alpha = _alpha_channel(image)
    if alpha is None:
        return
    histogram = alpha.histogram()
    opaque_pixels = sum(histogram[1:])
    total_pixels = sum(histogram)
    if opaque_pixels == 0:
        raise MediaConversionError(
            "imagem totalmente transparente (sem pixels opacos); troque por uma imagem com fundo"
        )
    if opaque_pixels < _MIN_OPAQUE_FRACTION * total_pixels:
        raise MediaConversionError(
            "imagem quase totalmente transparente "
            f"({opaque_pixels / total_pixels:.2%} de pixels opacos); troque por uma imagem com fundo"
        )


def _flatten_if_transparent(image: Image.Image) -> Image.Image:
    """Aplica a politica de transparencia: rejeita imagem vazia, achata o resto."""
    _reject_if_empty_alpha(image)
    return flatten_transparency(image)


def _cap_inline_width(image: Image.Image) -> Image.Image:
    """Downscale inline images wider than MAX_INLINE_WIDTH (1280px).

    Posters and full-bleed art often arrive at 2000-5000px wide; publishing
    them at full size bloats the page and hurts performance for no visible
    gain. Images are only ever scaled DOWN to at most 1280px wide (aspect
    ratio kept) — smaller sources are never upscaled. The 64px minimum check
    still applies before this call.
    """
    if image.width <= MAX_INLINE_WIDTH:
        return image
    height = max(1, round(image.height * MAX_INLINE_WIDTH / image.width))
    return image.resize((MAX_INLINE_WIDTH, height), Image.Resampling.LANCZOS)


def convert_to_webp(source: Path, destination: Path | None = None) -> Path:
    source = Path(source)
    destination = destination or source.with_suffix(".webp")
    try:
        image = _open_authoritative(source)
        with image:
            if image.width < 64 or image.height < 64:
                raise MediaConversionError("image resolution is below 64x64")
            if image.width < MIN_INLINE_WIDTH:
                raise MediaConversionError(
                    f"inline image source is {image.width}px wide (minimum {MIN_INLINE_WIDTH}px); "
                    "pick a larger source — content images are never upscaled"
                )
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA")
            # Politica de transparencia: imagem vazia e rejeitada; o resto e
            # achatado sobre branco — o WebP publicado nunca tem canal alpha.
            image = _flatten_if_transparent(image)
            image = _cap_inline_width(image)
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
    """Center-crop and resize to exactly 1280x720 (16:9) and save as WebP.

    Featured images must always obey the portal's 1280x720 ratio, so the
    source is cover-cropped from its center and resized to the exact target
    (upscaling allowed so the guaranteed size holds for small sources).

    The EXIF orientation is applied first (a photo stored sideways would
    otherwise be published lying down) and PORTRAIT sources are rejected:
    cover-cropping a portrait into 16:9 destroys more than half the frame, so
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
                    "a 1280x720 featured image requires a landscape source — pick landscape key art"
                )
            work = image.convert("RGBA") if image.mode not in {"RGB", "RGBA"} else image
            # Politica de transparencia: imagem vazia e rejeitada; o resto e
            # achatado sobre branco — o destaque publicado nunca tem alpha.
            work = _flatten_if_transparent(work)
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
