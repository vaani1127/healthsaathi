# ADR 0006: Anchoring, receipts and verification

Status: accepted

## Context

SPEC 6 asks for signed checkpoints of each clinic's audit log to be published to public witnesses
(Polygon Amoy, a GitHub witness repository and OpenTimestamps), and for patients to check their
own access receipts in the browser. A few details are left open: how the scheduled job reaches the
database, what happens when two runs overlap, and what exactly a receipt contains.

## Decision

**One job, three triggers.** `app.anchor.job.run()` does one anchoring pass. It runs from the CLI
(`python -m app.anchor.job`), from `POST /api/v1/internal/anchor/run` (called every 15 minutes by
`.github/workflows/anchor.yml` with `ANCHOR_TRIGGER_TOKEN`), and from the worker when no
checkpoint or anchor attempt has happened for `ANCHOR_FALLBACK_MINUTES`. The GitHub runner calls
the API instead of the database because the production database is not reachable from outside
the VM.

**Overlap is safe.** A session-level Postgres advisory lock lets only one run proceed; a second
run returns at once. Every step is recorded in `anchor_receipts` before and after it happens, so a
run that stops halfway is finished by the next one. A sent but unmined transaction is looked up
again instead of being sent twice.

**Own database role.** The job runs as `anchor_job`, which can read every clinic's audit events
and write checkpoints and anchor receipts, but cannot touch clinical tables.

**History rewrites are refused.** Before posting a new root, the job checks that the root already
on chain is still the root of the same-sized prefix of today's log. If not, it marks the attempt
failed, logs a critical error and stops for that clinic. The contract also requires the previous
root, so a rewritten log cannot be anchored even by a misbehaving job.

**Witness failures are isolated.** One witness failing (RPC down, GitHub error, calendar offline)
does not stop the others or the app.

**Receipt contents.** A receipt holds the audit payload for the access, its payload hash and leaf
hash, the inclusion proof, the latest signed tree head and its signature, the clinic public key,
the on-chain clinic id and the anchor references. The browser checks the payload hash, the leaf
hash, the inclusion proof and the signature offline, then reads `ClinicRegistry.signerKeyHash` and
the `Anchored` event (or `AuditAnchor.latest`) over RPC with viem. Opening a receipt is itself a
recorded access. Receipts are only for the patient the access was about.

**Shared vectors.** `make vectors` writes a receipt in the exact API format into
`packages/receipt-verify/test/vectors/ledger.json`. Python checks that it matches the API model and
TypeScript checks that it verifies, so the two sides cannot drift apart.

## Consequences

- Wallets, test POL, the RPC URL, the witness repository and the contract addresses are manual
  set-up steps. With none of them set, the job still signs checkpoints and skips the witnesses.
- Building a receipt rebuilds the clinic's Merkle tree from its audit rows. This is fine at the
  size of a small clinic; a cached tree can be added later if it becomes slow.
- `make tamper-demo` shows the check working on a local anvil chain.
