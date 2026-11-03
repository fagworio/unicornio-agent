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

### Aggregate journal format and integrity

`work/v2-cycle/journal.jsonl` is an append-only history and may contain events
from more than one cycle. New events are JSONL; the read-only parser also
accepts valid historical pretty-printed JSON objects separated by JSON
whitespace. Reading an older format never normalizes or rewrites it. Before a
checkpoint save or journal append, the writer takes the journal file lock and
validates the complete existing stream. It adds a record delimiter when a
valid historical object has no trailing newline.

Invalid UTF-8, truncated or malformed objects, duplicate object keys, exact
duplicate event records, invalid event identities, or conflicting
`cycle_id`/round/authorization/selection values fail closed. A damaged journal
is not repaired by adding a newline or skipping a bad record: preserve its
bytes and investigate from a read-only copy. Since the file can contain
multiple cycles, authorization is correlated using the composite identity
`cycle_id + round_number + authorization_id`; an event from another cycle is
historical evidence, not authorization for the current checkpoint.

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
after reviewing the evidence. The apply call must also include the exact device,
inode, mtime, and raw-record hash from that same preview:

```bash
PYTHONPATH=src .venv/bin/python -m unicornio_editor.cli \
  v2-lock-reconcile POST_ID --root "$ROOT" --apply \
  --expected-sha256 OWNER_RECORD_SHA256 \
  --expected-device DEVICE --expected-inode INODE \
  --expected-mtime-ns MTIME_NS --expected-bytes-sha256 RAW_BYTES_SHA256
```

The apply path reacquires the kernel lock and revalidates the exact record and
process identity. It does not touch WordPress, cycle checkpoints, journals, or
the MediaRegistry. Repeated apply is idempotent.

An unreleased owner record with missing or unverifiable host/process identity
is not eligible for this stale-owner command, even when its timestamp is old
and the kernel lock is currently free. `LockManager.acquire()` will not
replace it based on TTL. The separate legacy command is preview-only by
default:

```bash
PYTHONPATH=src .venv/bin/python -m unicornio_editor.cli \
  v2-lock-legacy-reconcile POST_ID --root "$ROOT" \
  > /caminho-protegido/legacy-lock-preview.json
```

Applying a legacy proposal requires the exact saved preview, an external proof
bundle, and explicit operator/authorization identifiers:

```bash
PYTHONPATH=src .venv/bin/python -m unicornio_editor.cli \
  v2-lock-legacy-reconcile POST_ID --root "$ROOT" --apply \
  --preview-evidence /caminho-protegido/legacy-lock-preview.json \
  --external-proof /caminho-protegido/external-proof.json \
  --authorized-by OPERADOR --authorization-id TICKET_AUDITAVEL
```

The external proof JSON must bind the same `post_id`, `owner_record_sha256`,
`owner_record_bytes_sha256`, `owner_device`, `owner_inode`, and
`owner_mtime_ns` from the preview, and assert `owner_source_verified: true`,
`writer_processes_stopped: true`, plus a `proof_reference_sha256` identifying
the separately retained evidence. The CLI checks the structure and exact
snapshot binding; it cannot independently verify those external assertions or
the truth of the operator's authorization. Do not fabricate the proof file.
The apply path reopens and locks the file, repeats the byte/inode/mtime CAS,
writes a redacted private audit record, updates the owner record in place, and
checks readback. An interrupted or divergent audit stays blocked for review.
Releasing a per-post lock is not proof that a cycle, Writer, WordPress post,
or MediaRegistry operation is safe to replay; it does not resume a cycle or
authorize a post write.

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
