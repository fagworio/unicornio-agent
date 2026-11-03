# V2 cycle investigation and safe quarantine release

The investigation queue is a read-only operational view. It correlates each
quarantined post with its completed isolation transition and, when a WordPress
client is configured, re-runs the production evidence classifier against the
current journal, MediaRegistry V5, and WordPress readback. It never changes a
checkpoint, journal, Registry row, or WordPress post.

## Auditing an active pending round

Before any recovery decision, use the audit command against the installation
root. It reports that checkout's exact Git SHA, cycle/authorization identity,
all selected post IDs, the production recovery classifier's result for each
post, Registry and journal fingerprints, WordPress readbacks, and lock status.
It reads each selected post twice and compares the checkpoint, aggregate
journal, quarantine file, and lock observations before and after. Any missing,
busy, ambiguous, or changing evidence fails closed.

```bash
PYTHONPATH=src .venv/bin/python -m unicornio_editor.cli \
  v2-audit-pending-round --root "$ROOT"
```

This command is strictly a preflight: it does not release quarantine, reconcile
the checkpoint, run an editorial cycle, or write to WordPress or the Registry.
`safe_for_operator_review` only means the evidence snapshot is stable and the
Registry schema is complete; `automatic_recovery` remains false. Compare
`git_sha` with the intended release and review every selected post before any
separate, explicitly authorized operation. Do not treat a successful fixture
or a local checkout SHA as evidence of the production installation.

If the checkpoint is interrupted before selection authorization, the audit can
return `preauthorization_interrupted` / `SAFE_TO_RESUME_PREVIEW`. That result
requires one matching `round_started` event and no later authorization event;
it has no selected IDs and does not authorize writes. The normal cycle may
resume only its preview, archives the checkpoint, cycle journal, Registry
snapshot (when present), and preview evidence, then creates an authorization
only after persisting the selected IDs. Any checkpoint/journal divergence stays
blocked as `recovery_uncertain`.

If interruption occurs after selection and the durable
`round_authorization_prepared` event but before `round_authorized`,
`v2-audit-pending-round` performs two read-only evidence passes for every
selected post. It reports `authorization_prepared_recoverable` only when the
checkpoint and journal remain unchanged, every post is classified
`NOT_EXECUTED`, the complete Registry proves no operation after preparation,
WordPress readback remains `pending`, and all locks remain idle. A current
Writer journal, Registry operation, incomplete evidence, or changing snapshot
keeps the round blocked. This audit never completes the authorization or
executes Writer; the normal cycle may only resume the exact persisted selection
after the evidence has been reviewed.

## Reconciling a stale per-post lock record

Lock reconciliation is separate from cycle recovery and defaults to a
read-only preview. A record is eligible only when the kernel lock is free, the
record is intact, the owner host matches this machine, and process identity
proves the original owner exited (including PID-reuse detection). Missing or
legacy identity fields, an active process, a different host, or a busy lock
remain blocked. The operation never unlinks the lock file and preserves a
redacted, mode-0600 audit record before marking the old owner record released.

```bash
PYTHONPATH=src .venv/bin/python -m unicornio_editor.cli \
  v2-lock-reconcile POST_ID --root "$ROOT"
```

Apply only the exact `owner_record_sha256` returned by the preview, and only
after reviewing the evidence:

```bash
PYTHONPATH=src .venv/bin/python -m unicornio_editor.cli \
  v2-lock-reconcile POST_ID --root "$ROOT" --apply \
  --expected-sha256 OWNER_RECORD_SHA256
```

The apply path reacquires the kernel lock and revalidates the exact record and
process identity. It does not touch WordPress, cycle checkpoints, journals, or
the MediaRegistry. Repeated apply is idempotent.

```bash
PYTHONPATH=src .venv/bin/python -m unicornio_editor.cli \
  v2-investigation-queue --root "$ROOT"
```

The queue reports the isolation age, original authorization, current safe
classification, bounded evidence hashes/statuses, next action, cause counts,
and alerts for an overdue or unusually large queue. It does not include raw
candidate URLs, article content, or unredacted evidence references.

## Releasing one isolated post

Release is never automatic. The operator first reviews the queue and produces a
fresh preview for one post, with an explicit authorization ID, operator, and
reason. The command acquires the normal cycle and post locks, performs reads,
and emits a snapshot hash. The preview must be retained locally for the
subsequent apply:

```bash
PYTHONPATH=src .venv/bin/python -m unicornio_editor.cli \
  v2-release-isolated POST_ID --root "$ROOT" \
  --authorization-id AUTH_ID --authorized-by OPERATOR --reason 'REVIEW_REASON' \
  > /tmp/v2-release-POST_ID-preview.json
```

Apply requires the exact preview file and repeats the classification under
lock. Any change in the isolation transition, quarantine policy, journal,
Registry, or WordPress readback blocks the release. Only
`COMMITTED_NO_REPLAY`, `PRE_WRITER_RECOVERABLE`, and `NOT_EXECUTED` with complete
no-write proof can be considered. An uncertain writer, unavailable evidence,
incomplete transition, active lock, or legacy identity mismatch remains
quarantined. Apply removes only that post from quarantine and appends an
auditable release-history record; it does not replay a writer, modify
WordPress, or rewrite cycle history.

Pass the saved preview JSON back with `--preview-evidence PATH` and add
`--apply` to the same command only after the independent operator decision.
Changing the authorization ID, operator, or reason invalidates the preview.

## Media acquisition funnel

The report reads the local MediaRegistry SQLite file in SQLite read-only mode;
it does not load WordPress credentials or migrate the schema. It reports
aggregate and per-post counts for `SEARCH_INCOMPLETE`, source/provenance
failures (including `source_mismatch` and `unresolved_source`), and rejected
featured candidates. It excludes raw URLs, content, and `evidence_reference`.

```bash
PYTHONPATH=src .venv/bin/python -m unicornio_editor.cli \
  v2-media-funnel-report --root "$ROOT" --since-days 7
```

These metrics describe evidence recorded in that installation's Registry.
They do not prove production health unless collected from the current
production installation and correlated with its exact code SHA and time
window. A missing or incomplete Registry is reported as unavailable, never as
zero failures.

## Production boundary

These commands do not perform a production deploy or trigger an editorial
cycle. Before investigating or releasing a production isolation, the operator
must confirm the installed SHA, current checkpoint and transition, all five
round members, absence of active writer locks, and current WordPress/Registry
evidence. If any of those facts are unavailable or divergent, preserve the
quarantine and stop. A successful local fixture is not evidence that a
production post has been safely released or that Publisher completed a
publication.
