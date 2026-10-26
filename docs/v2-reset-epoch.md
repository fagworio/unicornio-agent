# v2-reset-epoch

`v2-reset-epoch` is an explicit administrative migration for V2. It is not a retry-all command and it does not delete operational history.

## Preview

```bash
.venv/bin/unicornio-editor v2-reset-epoch \
  --root . --preview --report-dir /root/.hermes/cache/scratch/reset-preview
```

Preview is read-only. It paginates `pending` and `awaiting_human`, preserves published/non-editorial posts, protects quarantine and uncertain external effects, computes a canonical snapshot hash, and writes `preview.json` to the report directory.

## Apply

Apply requires the exact current preview and a non-empty operator authorization:

```bash
.venv/bin/unicornio-editor v2-reset-epoch \
  --root . --apply \
  --preview-evidence /root/.hermes/cache/scratch/reset-preview/preview.json \
  --operator-authorization AUTHORIZATION_ID \
  --report-dir /root/.hermes/cache/scratch/reset-apply
```

The command refuses stale evidence, changed posts, changed checkpoint/quarantine/Registry fingerprints, active V2/Publisher processes, missing locks, invalid quarantine, or invalid state. It creates a durable epoch transition and archive, backs up SQLite with `Connection.backup()`, projects only reusable assets/qualification decisions into the active Registry, archives old cycle/journal/backups, resets safe posts using canonical `WorkState`, confirms WordPress readback, and creates a new `CycleStateStore` checkpoint without a pending round.

Posts with publication or uncertain external effects are excluded. In particular, `115757` remains protected and is never replayed. The command does not alter posts outside the frozen preview, does not delete attachments, and does not publish.

The apply path is intentionally not called by tests or deployment automatically. A production apply must follow a fresh preview, verified backup, explicit operator authorization, paused writers, and an independent readback review.
