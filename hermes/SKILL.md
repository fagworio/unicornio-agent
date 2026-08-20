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
3. Produce strict editorial JSON with `site_relevance`, `cleaned_html`, `seo`, `media_plan`, and trailer fields.
4. Google Images is only discovery. Verify the original page and a public-domain, compatible Creative Commons, or explicit permission license.
5. Record source page, author, license, license URL, capture time, and visible credit. Reject uncertain images.
6. Use local WordPress Media Library uploads only; do not use external buckets/CDNs or hotlinks.
7. Run `unicornio-editor apply POST_ID editorial.json`.
8. Inspect the JSON result and backup path. A `skip` or dry-run result must have `wordpress_changed=false`.

The agent never changes a post to a publishing status. All media credits must remain visible and traceable to the license evidence.
