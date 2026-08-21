#!/usr/bin/env python3
"""Re-normalize featured images of the 6 blocked posts using their ORIGINAL
attachments (real images already in the WordPress Media Library).

For each post: download the original featured attachment, convert to exactly
1280x720 WebP, upload as a NEW attachment whose filename derives from the
original source (provenance evidence preserved — post-fix behavior), and set
it as the post's featured_media. No image is invented or created: all sources
are real attachments already present in the Media Library.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path("/www/wwwroot/hermes/unicornio-agent")
sys.path.insert(0, str(ROOT / "src"))

from unicornio_editor.config import load_config  # noqa: E402
from unicornio_editor.media.converter import prepare_featured_webp  # noqa: E402
from unicornio_editor.media.downloader import download_image  # noqa: E402
from unicornio_editor.workflow import _featured_filename_from_source  # noqa: E402
from unicornio_editor.wordpress import WordPressClient  # noqa: E402

# post_id -> original attachment id (real image the imported post carried)
PLAN = {
    109122: 109123,  # 5-Classic-Anime-That-Deserve-Remakes (arte da materia)
    109138: 109139,  # the-sinking-city-2-game (key art de um dos 7 jogos)
    109168: 109169,  # Remothered-Red-Nuns-Legacy key art
    109170: 109171,  # Takopis-Original-Sin key art
}


def _rendered(value) -> str:
    if isinstance(value, dict):
        return str(value.get("rendered") or "")
    return str(value or "")


def main() -> int:
    config = load_config()
    client = WordPressClient(config)
    for post_id, media_id in PLAN.items():
        media = client.get_media(media_id)
        source_url = (media.get("source_url") or "").strip()
        title = _rendered(media.get("title")).strip() or "Imagem de destaque"
        alt = str(media.get("alt_text") or "").strip()
        caption = _rendered(media.get("caption")).strip()
        if not source_url:
            print(json.dumps({"post_id": post_id, "status": "error", "reason": "sem source_url"}))
            continue
        filename = _featured_filename_from_source(source_url)
        with tempfile.TemporaryDirectory(prefix=f"fix-featured-{post_id}-") as directory:
            tmp = Path(directory)
            try:
                source = download_image(source_url, tmp / "src.jpg")
                webp = prepare_featured_webp(source, tmp / "featured.webp")
                new_media = client.upload_media(
                    str(webp),
                    filename=filename,
                    alt_text=alt,
                    title=title,
                    caption=caption,
                )
                new_id = new_media.get("id")
                if not isinstance(new_id, int):
                    print(json.dumps({"post_id": post_id, "status": "error", "reason": "upload sem id"}))
                    continue
                client.update_post(post_id, {"featured_media": new_id})
                print(json.dumps({
                    "post_id": post_id,
                    "status": "ok",
                    "original_media": media_id,
                    "new_media": new_id,
                    "filename": filename,
                    "url": new_media.get("source_url"),
                }, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                print(json.dumps({"post_id": post_id, "status": "error", "reason": str(exc)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
