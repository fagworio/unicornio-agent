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

1. Run `unicornio-editor cards` — ONE call returns the compact cards of the
   pending batch (title, word count, distinctive entities, original link,
   featured/seo/image gaps, preserved-image count, game hint, state). It
   replaces list-pending + prepare + file reads; write the editorial JSONs
   straight from the cards. `list-pending --compact` remains for quick peeks.
   For the overall queue state (pending x already-edited x old backlog), run
   `unicornio-editor queue` — read-only, deterministic. The cron monitor
   (`hermes` monitor_script -> `queue --monitor`) only wakes this agent when a
   NEW recent pending post appears or one is processed; idle ticks cost zero
   tokens. Old pending posts (outside the 7-day window) are intentionally NOT
   monitored so stale content never floods the publish flow.
2. Run `unicornio-editor prepare POST_ID --compact`.
3. Produce strict editorial JSON with `site_relevance`, `seo`, `media_plan`, and trailer fields. When the post is about a game, set `game_name` to the exact game name (the code deterministically finds and validates the YouTube trailer — never invent trailer URLs); otherwise `game_name: null`. `cleaned_html` is OPTIONAL: omit it (or set null) when the prepared text is already good — `apply` then reuses the deterministic cleaned content (no-rewrite path); include it ONLY when you actually rewrite the text.
4. Google Images is only discovery. Verify the original page hosting the image (a Google Images
 preview URL is never a source). IMAGE POLICY (2026-08): any web image may be used as long as a
 VISIBLE CREDIT is attached — the credit block is the evidence. Free licenses (CC0, CC BY,
 public domain, permission granted) remain preferred and are accepted as before; for every
 SEARCH ORDER (editor rule 2026-08-21): start with the WEB — Google/web_search to find the
 key art on news sites, official pages, stores (Steam CDN header.jpg, etc.) — extract the
 DIRECT image URL from the original page and use `license: "Uso com crédito"` with
 `license_url` = the original page. Wikimedia Commons is a FALLBACK, not the default: it has
 little official key art and rate-limits aggressively (HTTP 429). Do NOT get stuck retrying
 Wikimedia — switch to web sources immediately.
   other image mark the candidate `license: "Uso com crédito"` and `license_url` as the original
   image page (empty license_url falls back to the source page automatically). Do NOT write
   "All Rights Reserved" as the license — use the use-with-credit marker.
5. Record source page, author, license, license URL (optional for use-with-credit), capture time,
   and the visible credit. Every image must carry `Crédito da imagem: ...` (work/author +
   description + license note) — the credit IS the policy guarantee.
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
   FEATURED IMAGE = THE SUBJECT ITSELF (mandatory): the featured image must be key art / an
   official image OF the game or work cited — never a tangential symbol (a Disney castle is NOT
   a Kingdom Hearts featured image, even if the castle appears in the game). The code gate runs
   the featured candidate through `image_is_relevant(..., source_only=True)`: only the real
   source file/page name counts as evidence, so pick images whose FILE NAME carries the
   game/work name (e.g. `Kingdom_Hearts_wordmark_....png`), not a generic caption the agent
   could write over a wrong image. Checklist item `destaque_relevancia` re-validates this at
   publish time.
   EXISTING FEATURED IS REUSED (2026-08-21): when the post ALREADY has a
   featured image and the `media_plan` has no approved featured item, the
   apply REUSES the existing one via `_normalize_existing_featured` — it
   re-downloads the source, converts to exactly 1280x720 WebP and uploads a
   NEW attachment whose FILE NAME derives from the original source (e.g.
   `remothered-red-nuns-legacy-...-1280x720.webp`), preserving provenance so
   the relevance gate still matches. Therefore: if the post's current
   featured is the correct key art (file/page name carries the work), OMIT
   `is_featured` from the `media_plan` and let the code normalize it. Only
   provide a new featured item when the existing one is wrong or missing.
   The checklist `destaque_relevancia` now validates the REAL attachment
   (url+title+alt), not the media_plan intent.
   LISTICLE H2s NAME THE WORKS: the entity extractor reads the H2 headings
   ("1. Tokyo Ghoul: ...", "3. The Sinking City 2: ..."), so a list post
   with a generic title still matches images of the cited works. Inline
   alts/credits for list items MUST carry the exact work name from the H2.
   FEATURED = KEY ART OF A CITED WORK, NEVER ARTICLE ART (editor rule
   2026-08-21): the model CANNOT see the image — a filename can pass the
   text gate while the file is actually an article header/wordmark (e.g. a
   "5 Classic Anime That Deserve Remakes" banner). The featured image must
   depict ONE OF THE WORKS CITED IN THE POST (key art/logo/official art of
   a game/anime/film named in the title or in an H2). For lists without a
   good existing featured, use the key art of the LAST listed work. Never
   reuse an existing featured that is generic article art; the code now
   refuses to re-normalize a featured whose real evidence (url/title/alt)
   matches no cited work, so provide a new `is_featured` media item with
   real key art (file name carrying the work) whenever the existing one is
   not a cited-work image.
   FEATURED MUST BE LANDSCAPE (mandatory): the converter applies EXIF orientation (photos stored
   sideways are transposed, never published lying down) and REJECTS portrait sources with
   `MediaConversionError` — a 1280x720 featured image cannot come from a portrait photo. Always
   choose landscape key art/wordmarks (w >= h) for `is_featured: true`.
   SIZE RULES (portal definition): featured images are prepared at exactly 1280x720 WebP (16:9).
   INLINE images are capped at 1280px WIDE — posters/art wider than 1280 are scaled down (aspect
   kept), smaller sources are never upscaled. Oversized images hurt page performance; never
   request or accept a source that would force a huge upload.
   MEDIA LIBRARY REUSE (preferred before external discovery): run
   `unicornio-editor media-search TERMO --limit N` (read-only) to find images already in the
   local Media Library that fit the post. A candidate is reusable ONLY when `tem_credito: true`
   (its title/caption carries the `Crédito da imagem:` block = license evidence). Reference it in
   the media_plan item as `media_library_id: <id>` (plus the normal author/license/credit fields
   copied from its title/caption). The apply downloads the attachment file and uploads a NEW
   attachment — the original title/alt/caption are NEVER overwritten. External discovery
   (Google Images/Wikimedia) remains the fallback when the library has nothing fitting.
6. Use local WordPress Media Library uploads only; do not use external buckets/CDNs or hotlinks. Featured images are mandatory and are prepared at exactly 1280x720 WebP (`is_featured: true` on at most one media_plan item).
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
- OMIT `seo` from the editorial JSON when the post already has valid Rank Math meta (the card's
  `seo_exists` is true): the code inherits it. Only provide `seo` when `seo_exists` is false.
- NEVER include the CTA, the Fonte or any footer in `cleaned_html` — `append_canonical_footer`
  inserts exactly one canonical CTA + Fonte and strips any duplicates. Writing them costs tokens
  and risks duplication.
- Conservative skip (irreversible-loss protection): skip ONLY with confidence >= 0.9 (the card
  shows nothing; apply refuses low-confidence skips and writes `backups/<id>/uncertain.json`
  instead — the post stays pending, out of the queue, for later review). Uncertain content:
  do NOT apply it as a final skip.
- Topic gate: `matched_topics` must intersect the site topics (`SITE_TOPICS`); the checklist
  fails otherwise. Pick topics from the site list (games, xbox, playstation, nintendo, anime,
  cultura geek, streaming, series, cinema, filmes, tecnologia, ...).
- Image alt texts MUST name the subject/work (e.g. "Redfall key art"), never generic labels
  like "Imagem do jogo" — the relevance gate rejects generic alts anyway.
- Validate before writing: `unicornio-editor apply POST_ID editorial.json --dry-run` runs the
  full checklist + preview without touching WordPress; use it to fix gaps before the real apply.
- When the media plan does not add real reading value, do not search for images at all.

## Operational pitfalls (learned in production)

- The CLI reads env vars directly (no dotenv). If the shell lacks the `.env` values, it falls back to the dev mock URL (`http://wordpress.dvl.to:8080`) and "times out". Always run with the project env sourced, without printing values:
  `set -a && . ./.env && set +a && .venv/bin/unicornio-editor ...`
- `list-pending` must query `status=pending` server-side (fixed 2026-08-20): on production the unfiltered `/posts` listing hides non-published statuses, so local filtering always returns `[]`.
- `list-pending` returns the newest pending post first (`per_page=EDITOR_BATCH_LIMIT`). Progress is tracked by snapshots under `backups/<post_id>/` — check that dir to see which posts were already processed.
- Production REST works behind Cloudflare with application-password auth as WP user `redacao-agent` (see .env; never print the password).
