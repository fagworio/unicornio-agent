---
name: unicorniohater-editor
description: Process WordPress pending posts in write mode, gated by the pre-publish checklist.
version: 0.1.0
metadata:
  hermes:
    tags: [wordpress, editorial, pending, safety]
---

# UnicornioHater Editorial Agent

Process only WordPress posts with status `pending`.

## Non-negotiable safety

- Production runs in write mode (`EDITOR_DRY_RUN=false` in the .env): `apply` really writes the editorial content and meta, always keeping status `pending`. Publishing happens ONLY through the publish cron (`publish-ready`), gated by `PUBLISH_ENABLED=true` and a fully passing checklist.
- Never send `status` in an update payload.
- Re-fetch the post immediately before any write and abort if its status is not `pending`.
- Skip irrelevant or uncertain content without changing WordPress.
- Create a JSON snapshot before processing.
- Never log credentials, tokens, cookies, or full authorization headers.
- Never use production posts for local validation.

## Editorial flow

1. Run `unicornio-editor list-pending --compact` (economy mode; see below).
   For the overall queue state (pending x already-edited x old backlog), run
   `unicornio-editor queue` — read-only, deterministic. The cron monitor
   (`hermes` monitor_script -> `queue --monitor`) only wakes this agent when a
   NEW recent pending post appears or one is processed; idle ticks cost zero
   tokens. Old pending posts (outside the 7-day window) are intentionally NOT
   monitored so stale content never floods the publish flow.
2. Run `unicornio-editor prepare POST_ID --compact`.
3. Produce strict editorial JSON with `site_relevance`, `seo`, `media_plan`, and trailer fields. When the post is about a game, set `game_name` to the exact game name (the code deterministically finds and validates the YouTube trailer — never invent trailer URLs); otherwise `game_name: null`. `cleaned_html` is OPTIONAL: omit it (or set null) when the prepared text is already good — `apply` then reuses the deterministic cleaned content (no-rewrite path); include it ONLY when you actually rewrite the text.
4. Google Images is only discovery. Verify the original page and a public-domain, compatible Creative Commons, or explicit permission license.
5. Record source page, author, license, license URL, capture time, and visible credit. Reject uncertain images.
   IMPORTED IMAGES: inline images already in the post that carry a complete credit block inside the
   figure (`Crédito da imagem: <autor>. <descrição>. Licença <CC|CC0|domínio público> (<url da licença>)`)
   are PRESERVED automatically by `clean_html` — deterministic code validation, no AI and no web
   work. Do NOT re-discover or re-upload them, and do NOT include them in `media_plan`. Only images
   whose credit is missing/incomplete are removed and require `media_plan` rediscovery.
   IMAGE RELEVANCE (mandatory): every image must depict the EXACT subject cited — the work,
   character, object or person — never a generic concept that merely shares a keyword. A post
   about the game Redfall (vampires) must use Redfall key art/screenshots; a real bat photo is
   REJECTED. The code gate (`media/relevance.py`) rejects any candidate whose alt/credit/source
   URL has zero overlap with the post's distinctive entities (concept words like vampiro, jogo,
   anime, convencao never count). If no truly related image can be found, leave the slot out of
   `media_plan` — no image beats a wrong image. The checklist item `relevancia_imagens` blocks
   publication when any inline image is unrelated, and the 2/4/6 minimum is waived for posts with
   zero images (relevance-first policy).
6. Use local WordPress Media Library uploads only; do not use external buckets/CDNs or hotlinks. Featured images are mandatory and are prepared at exactly 1200x720 WebP (`is_featured: true` on at most one media_plan item).
7. Run `unicornio-editor apply POST_ID editorial.json`.
8. Run `unicornio-editor checklist POST_ID editorial.json` (read-only) and only publish when every item passes — backup, pending status, relevance, content, Fonte (original_link), body images per length (2/4/6), mandatory featured image, WebP, trailer (if game), CTA, text quality, structure, schema.
9. Inspect the JSON result and backup path. A `skip` or dry-run result must have `wordpress_changed=false`.
10. Publication flow: `apply` persists `backups/<ID>/editorial.latest.json`; only the publish cron
    (`hermes/publish-cron.sh` -> `unicornio-editor publish-ready`, gated by PUBLISH_ENABLED=true)
    may publish, after re-running the full checklist. Never publish manually outside this flow.

The agent never changes a post to a publishing status. All media credits must remain visible and traceable to the license evidence.

## Token economy (cron runs — every token costs money)

These rules exist because every execution runs against a paid API. Following them cuts per-run cost
several-fold. They are mandatory, not style advice.

- Use `list-pending --compact` ALWAYS. The full mode dumps the entire content of every post
  (~120 KB for a batch of 5) into the conversation; the compact mode prints only id, title, date,
  word_count and link (~1.3 KB). You never need the full post to choose the next post.
- Use `prepare POST_ID --compact` ALWAYS. It writes the full prepared JSON to
  `backups/<ID>/prepared.json` and prints a short summary. When you need the `cleaned_html` to
  rewrite the text, read the file with `read_file` (it is ~6 KB, read it ONCE). Do not re-run
  `prepare` to recover content you already saw.
- NEVER read project source files (`src/**`, `pyproject.toml`, `.env`, tests) to "understand" the
  flow. The CLI is the interface and this skill documents the flow. Reading the source once costs
  thousands of tokens per run for zero decision value. The only exception: a CLI error that is not
  self-explanatory — then read ONLY the function mentioned in the traceback.
- NEVER dump downloaded HTML into the terminal. Save pages to `/tmp` (`curl -s URL -o /tmp/p.html`)
  and extract only the needed fragment (license, author, date) with `grep`/`search_files`.
  A single full-page dump (e.g. 350 KB) can exceed the entire rest of the run.
- Do not run the same CLI command twice. Keep one result per command in the conversation and reuse
  it. Each run of `list-pending` costs ~1.3 KB compact (vs ~120 KB full).
- If `list-pending` returns an empty list, stop immediately — do not explore, do not re-check.
- Process posts one at a time: `prepare` -> editorial JSON (write it to a file) -> `apply` -> next.
  Never revisit a post that already has `backups/<ID>/editorial.latest.json` unless asked.
- Write the editorial JSON to a file with `write_file` and pass the path to `apply`/`checklist`;
  never paste the JSON body into the conversation more than once.
- OMIT `cleaned_html` from the editorial JSON unless you actually rewrote the text (no-rewrite
  default). The model re-emitting an unchanged article is pure output-token waste; the code reuses
  the prepared content deterministically.
- NEVER include the CTA, the Fonte or any footer in `cleaned_html` — `append_canonical_footer`
  inserts exactly one canonical CTA + Fonte and strips any duplicates. Writing them costs tokens
  and risks duplication.
- When the media plan does not add real reading value, do not search for images at all.

## Operational pitfalls (learned in production)

- The CLI reads env vars directly (no dotenv). If the shell lacks the `.env` values, it falls back to the dev mock URL (`http://wordpress.dvl.to:8080`) and "times out". Always run with the project env sourced, without printing values:
  `set -a && . ./.env && set +a && .venv/bin/unicornio-editor ...`
- `list-pending` must query `status=pending` server-side (fixed 2026-08-20): on production the unfiltered `/posts` listing hides non-published statuses, so local filtering always returns `[]`.
- `list-pending` returns the newest pending post first (`per_page=EDITOR_BATCH_LIMIT`). Progress is tracked by snapshots under `backups/<post_id>/` — check that dir to see which posts were already processed.
- Production REST works behind Cloudflare with application-password auth as WP user `redacao-agent` (see .env; never print the password).
