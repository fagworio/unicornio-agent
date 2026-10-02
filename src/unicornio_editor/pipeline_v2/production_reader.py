import json
from pathlib import Path
from typing import Any

from .snapshot import capture_snapshot


class ProductionShadowReader:
    """Read-only adapter exposing only GET post data and local manifests."""

    def __init__(self, wordpress_client, manifest_root: str | Path):
        self._wordpress = wordpress_client
        self._manifest_root = Path(manifest_root)

    def read_post(self, post_id: int) -> dict[str, Any]:
        return self._wordpress.get_post(post_id)

    def read_manifest(self, post_id: int) -> dict[str, Any]:
        path = self._manifest_root / str(post_id) / "editorial.partial.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def capture(self, post_id: int, output_dir: str | Path) -> Path:
        return capture_snapshot(post_id, self.read_post, self.read_manifest, output_dir)
