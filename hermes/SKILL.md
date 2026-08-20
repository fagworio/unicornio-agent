---
name: unicorniohater-editor
description: Process WordPress pending posts safely in dry-run-first mode.
version: 0.1.0
metadata:
  hermes:
    tags: [wordpress, editorial, pending, safety]
---

# UnicornioHater Editorial Agent

Process only WordPress posts with status `pending`.

## Non-negotiable safety

- Start and remain in `EDITOR_DRY_RUN=true` until multiple local runs are reviewed.
- Never send `status` in an update payload.
- Re-fetch the post immediately before any write and abort if its status is not `pending`.
- Skip irrelevant or uncertain content without changing WordPress.
- Create a JSON snapshot before processing.
- Never log credentials, tokens, cookies, or full authorization headers.
- Never use production posts for local validation.

## Editorial flow

1. Run `unicornio-editor list-pending`.
2. Run `unicornio-editor prepare POST_ID`.
3. Produce strict editorial JSON with `site_relevance`, `cleaned_html`, `seo`, `media_plan`, and trailer fields. When the post is about a game, set `game_name` to the exact game name (the code deterministically finds and validates the YouTube trailer — never invent trailer URLs); otherwise `game_name: null`.
4. Google Images is only discovery. Verify the original page and a public-domain, compatible Creative Commons, or explicit permission license.
5. Record source page, author, license, license URL, capture time, and visible credit. Reject uncertain images.
6. Use local WordPress Media Library uploads only; do not use external buckets/CDNs or hotlinks. Featured images are mandatory and are prepared at exactly 1200x720 WebP (`is_featured: true` on at most one media_plan item).
7. Run `unicornio-editor apply POST_ID editorial.json`.
8. Run `unicornio-editor checklist POST_ID editorial.json` (read-only) and only publish when every item passes — backup, pending status, relevance, content, Fonte (original_link), body images per length (2/4/6), mandatory featured image, WebP, trailer (if game), CTA, text quality, structure, schema.
9. Inspect the JSON result and backup path. A `skip` or dry-run result must have `wordpress_changed=false`.
10. Publication flow: `apply` persists `backups/<ID>/editorial.latest.json`; only the publish cron
    (`hermes/publish-cron.sh` -> `unicornio-editor publish-ready`, gated by PUBLISH_ENABLED=true)
    may publish, after re-running the full checklist. Never publish manually outside this flow.

The agent never changes a post to a publishing status. All media credits must remain visible and traceable to the license evidence.

## Operational pitfalls (learned in production)

- The CLI reads env vars directly (no dotenv). If the shell lacks the `.env` values, it falls back to the dev mock URL (`http://wordpress.dvl.to:8080`) and "times out". Always run with the project env sourced, without printing values:
  `set -a && . ./.env && set +a && .venv/bin/unicornio-editor ...`
- `list-pending` must query `status=pending` server-side (fixed 2026-08-20): on production the unfiltered `/posts` listing hides non-published statuses, so local filtering always returns `[]`.
- `list-pending` returns the newest pending post first (`per_page=EDITOR_BATCH_LIMIT`). Progress is tracked by snapshots under `backups/<post_id>/` — check that dir to see which posts were already processed.
- Production REST works behind Cloudflare with application-password auth as WP user `redacao-agent` (see .env; never print the password).
