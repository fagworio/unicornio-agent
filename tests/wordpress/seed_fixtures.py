#!/usr/bin/env python3
"""Seed only the local Devilbox WordPress with pending test posts.

Requires WORDPRESS_APP_USER and WORDPRESS_APP_PASSWORD in the environment.
The script refuses non-local hostnames and never changes post status from pending.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse


ROOT = Path(__file__).parent
FIXTURES = ROOT / "fixtures"
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "wordpress.dvl.to"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="create posts; without it only validates")
    parser.add_argument("--url", default=os.environ.get("WORDPRESS_URL", "http://wordpress.dvl.to:8080"))
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.hostname not in ALLOWED_HOSTS:
        raise SystemExit("refusing non-local WordPress host")
    fixtures = json.loads((FIXTURES / "scenario_matrix.json").read_text(encoding="utf-8"))
    credentials = (os.environ.get("WORDPRESS_APP_USER"), os.environ.get("WORDPRESS_APP_PASSWORD"))
    if args.apply and not all(credentials):
        raise SystemExit("WORDPRESS_APP_USER and WORDPRESS_APP_PASSWORD are required for --apply")
    for scenario in fixtures:
        fixture = json.loads((FIXTURES / scenario["fixture"]).read_text(encoding="utf-8"))
        if not args.apply:
            print(json.dumps({"scenario": scenario["name"], "action": "validated"}, ensure_ascii=False))
            continue
        payload = {
            "title": fixture["title"],
            "status": "pending",
            "content": fixture["content"],
            "meta": fixture.get("meta", {}),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        token = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode()
        request = Request(
            f"{args.url.rstrip('/')}/wp-json/wp/v2/posts",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Basic {token}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                created = json.loads(response.read())
        except (HTTPError, URLError) as exc:
            raise SystemExit(f"local fixture creation failed: {exc}") from exc
        if created.get("status") != "pending":
            raise SystemExit(f"WordPress returned unsafe status for {scenario['name']}")
        print(json.dumps({"scenario": scenario["name"], "post_id": created.get("id"), "status": created.get("status")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
