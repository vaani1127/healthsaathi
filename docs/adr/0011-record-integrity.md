# ADR 0011: Blockchain-anchored medical record integrity

Status: accepted (27 November 2026). Backend only; the web view, the TypeScript verifier and the
tamper demo follow in a later change.

## Context

Until now the ledger covered access events and a few admin and workflow events, not the clinical
record itself (`docs/record-integrity-audit.md`, findings 1 and 2). Nothing in the log or on a
witness said what a note, prescription, lab result or vital contained. Several clinical tables
could be changed in place by the app role (allergy and condition status, lab order status, lab
result release), and the database owner can change any row. So the claim that held was "the
access log is tamper-evident", not "the record is".

Constraints:

- No patient data on any chain or witness (CLAUDE.md rule 4).
- Existing leaves, roots and anchors must not change: they are on Polygon Amoy, the GitHub witness
  and OpenTimestamps, and the anchoring study (snapshot B) and the tamper study read them.
- `packages/e2d-core`, `sim/` and `experiments/` are frozen by the pre-registration. Nothing is
  added or edited there; `e2d_core.ledger.jcs.canonicalize` is imported, not changed.
- The explanation engine reads clinical tables as evidence, so their columns stay as they are.

## Decision

**Salted commitment per version.** Each committed version of a clinical record gets a random
32-byte salt and `commitment = SHA-256(salt || JCS(content))`. The salt stops anyone guessing a
small content (one blood pressure reading) from its commitment, lets the record holder check it
alone, and can be erased later to make one commitment meaningless without touching the log.

**Where it is kept.** A new append-only table `record_versions` (migration 0008) holds, per
version: clinic, patient, record type and id, version number, previous version id, content
schema, salt, commitment, the audit seq of its leaf, `backfilled`, author and time. Like
`audit_events`, the app role may only SELECT and INSERT, a trigger blocks UPDATE, DELETE and
TRUNCATE even for the owner, and RLS isolates clinics.

**New leaf type.** In the same transaction, one audit event of kind `record_version`:

```
{"type": "record_version", "v": 1, "record_type": "...", "record_id": "...", "version": n,
 "prev_version_id": "..." | null, "content_schema": "<type>/v1", "commitment": "<hex>",
 "by": "<user uuid>", "at": "<iso time>"}
```

plus `"backfilled": true` only on backfilled versions. No patient id, salt or content. It is an
ordinary audit payload: the hash chain, Merkle tree, signed tree heads, the AuditAnchor contract,
the GitHub witness, OpenTimestamps and patient receipts are unchanged, and only signed tree heads
leave the database, as before. A test proves a fixed log built with the code before this change
keeps its root and that record leaves only extend it.

**Content schemas.** `note/v1`, `prescription/v1`, `vital/v1`, `allergy/v1`, `condition/v1`,
`lab_order/v1`, `lab_result/v1`, `document/v1` (`apps/api/app/ledger/records.py`). Times are UTC
ISO 8601 with microseconds, decimals are strings with the column's scale, ids are strings. Notes
use the decrypted text, not the ciphertext (which changes when data keys rotate). Documents
include the file's SHA-256, so the attachment bytes are covered. A schema never changes once used;
a change gets a new name. The content is read back from the database before hashing, so the write
and every later check hash the same values. Lab result values that canonical JSON cannot encode
(NaN, Infinity, integers past 2^53) are refused with 422.

**When a version is committed.**

| Record | Committed |
|---|---|
| Note, prescription | On sign. Drafts are not part of the record. Each signed row is its own record with version 1, and `prev_version_id` points to the parent row's version. |
| Vital, document, lab order | On insert. A lab order's status is not in its content; its history is in `status_events`. |
| Lab result | On insert, and on release (version 2). |
| Allergy, condition | On insert, and on each status change that changes the value. |

The writer takes the clinic's audit-chain lock before choosing the version number. It is skipped
only in a load-test copy (`BENCHMARK_SKIP_RECORDING`), like the access log.

**Hardening (migration 0009).** vitals and documents become append-only (trigger for everyone,
UPDATE revoked from the app). lab_results refuses every UPDATE and DELETE except setting
`released_at` from NULL with every other column unchanged; the app may update only that column.
Notes and prescriptions get a unique index on `parent_id`, so two versions cannot be added to the
same parent at once (the service answers 409). Before building the index, a read-only guard looks
for duplicates and stops the migration with a clear message if any exist.

**Check.** `GET /api/v1/records/{record_type}/{record_id}/integrity`, recorded through
`record_and_explain()` with the record type's resource and `view` (so the access policy decides:
for example reception and nurses cannot check notes, and a patient only their own records). The
audit leaves are the reference. Verdicts:

- `match`: every stored version agrees with its leaf, the newest recomputes from the row as it is
  now, and its leaf is under a tree head confirmed on a chain (Amoy or Besu, as for receipts);
- `pending`: as match, but not yet under a chain-confirmed head;
- `mismatch`: the row differs from its newest commitment, a stored version differs from its leaf,
  a stored version has no leaf, or the log itself fails its hash checks;
- `missing_version`: a committable row has no version (written outside the app);
- `missing_row`: a committed record's row is gone (the patient is found from its version row).

For a note or prescription the check covers the whole version chain. Each version comes with its
leaf payload, leaf hash, inclusion proof to the signed tree head the receipts would use (the
selection is now one helper, `app/anchor/proofs.py`), the head's signature and its anchors, and
for the newest version the content and salt, so a browser can recompute everything. The admin
audit status gains a `records` line with counts per verdict and the number of backfilled versions;
mismatch or missing rows set its `problem`. The tamper study calls the same status function as the
anchor job role, which cannot read clinical tables, so the records line is computed only for the
admin page.

**Backfill.** Rows written before this change have no version. `make backfill-record-versions`
(`app/scripts/backfill_record_versions.py`, dry run unless `--apply`) writes version 1 and its
leaf for every committable row without one, both marked backfilled, notes and prescriptions
oldest version first. It is idempotent and runs in batches under the clinic's audit-chain lock.
A backfilled version shows only that the record has not changed since the backfill; the endpoint
reports `backfilled` per version and `covered_since` (the first version's time) so no one can
claim earlier coverage.

## Consequences

- The record claim becomes: each committed version of a clinical record is in the clinic's audit
  log as a salted commitment, under signed roots published on Polygon Amoy (testnet), a public
  GitHub repository and OpenTimestamps, so a later change to it, or its deletion, is evident to
  anyone holding the record. Wording follows SPEC 10: "tamper-evident", never "tamper-proof",
  "immutable" or "blockchain-secured".
- Limits that must be stated with the claim: a change made before the version's leaf is under an
  anchored root (the rewrite window measured by the anchoring study) is not evident; the check
  shows a record is unchanged since it was committed, not that it was true; someone with database
  access and the app can still add a new version, which is then visible in the history; drafts
  are not covered; a lab order's status is not covered (status_events is append-only but not in
  the log); backfilled records are covered only from the backfill time.
- Each clinical write adds one more audit event. Leaves per clinic grow, so `load_tree`, which
  re-hashes the whole clinic log for every receipt, every integrity check and every admin status
  refresh, gets slower. Not measured. The admin records line also reads and decrypts every
  committed row of the clinic on each refresh. Not measured either.
- A load-test copy skips version writes, as it skips access logging. A load run against this code
  "with recording" also writes version leaves, so it is not comparable with the committed
  `experiments/results/load` run, which predates this change.
- Old app versions run against the migrated schema (both migrations are additive for them), but
  rows they write get no version until the backfill runs again.
- No change to `packages/e2d-core`, `sim/`, `experiments/`, the contracts or any committed result.
  Anchoring snapshot B and the tamper study results are unaffected (see the CHANGELOG Deviations
  entry of 27 November 2026).
