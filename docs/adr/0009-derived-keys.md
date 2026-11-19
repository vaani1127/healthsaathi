# ADR 0009: One derived key per use of OTP_HMAC_KEY

Status: accepted (19 November 2026)

## Context

`OTP_HMAC_KEY` keyed five different things directly: email sign-in codes, staff invite tokens,
the access log's IP hash, consent commitments, and (through a small HMAC derivation) the rate
counter keys. One key for unrelated uses means a hash made for one purpose can be checked or
replayed in another, and a key cannot be changed for one use without the others. The other
server secrets were looked at too: `JWT_SECRET` has a single use, `DATA_KEYS` is a key ring whose
two uses are already separated by AES-GCM associated data, and `LEDGER_SIGNING_SEED` is already
turned into one Ed25519 key per clinic with HKDF (`e2d_core.ledger.sth.derive_clinic_key`).

## Decision

**Derivation.** `app/core/keys.py` derives one 32-byte key per use with HKDF-SHA256 (RFC 5869,
from the `cryptography` library), input `OTP_HMAC_KEY`, salt `healthsaathi/keys`, and a versioned
label as `info`. No new environment variables.

| Use | Label | Stored where |
|---|---|---|
| Email sign-in code | `healthsaathi/email-code/v1` | `email_otps.code_hash` (10 minutes) |
| Staff invite token | `healthsaathi/invite/v1` | `email_otps.code_hash` (72 hours) |
| Access log IP hash | `healthsaathi/ip-hash/v1` | `access_events.ip_hash` (append-only) |
| Consent commitment | `healthsaathi/consent-commitment/v1` | `consents.salt_hash` |
| Rate counter keys | `healthsaathi/rate-counters/v2` | `rate_counters.key` (15 minutes at most) |

A label changes version only when its key must change; the others are not affected. The module
hands out digests; the only function that returns key bytes is used to key a hash. Keys are
never logged and never returned by an endpoint (`tests/test_keys.py`).

**Not changed.** `JWT_SECRET` (one use; a new key would end every token in use for no gain),
`DATA_KEYS` (a new key would make stored notes and TOTP secrets unreadable; rotation already works
through key ids), `LEDGER_SIGNING_SEED` (already derived per clinic, and the public keys are
registered on chain). Refresh tokens are stored as plain SHA-256 with no secret and are not
affected; nobody is signed out.

**Cutover.** The first deploy that contains this change (on or after 25 November 2026):

- Email codes sent before it fail; the patient asks for a new one (they last 10 minutes).
- Invites sent before it keep working: accepting an invite matches the new hash or the old
  raw-key hash (`identity/service.py` `_legacy_invite_hash`). The legacy check is removed in a
  later release, 7 days after that deploy (`docs/RUNBOOK.md`).
- Rate counter keys change from v1 to v2, so the live counts start again once.
- Access events and consents written before it keep their values. Rows are never rewritten.

**Rule for old rows.** Rows carry no marker of which key made them. To check a stored consent
commitment or `ip_hash`, try the derived key first, then the legacy HMAC under the raw
`OTP_HMAC_KEY` (`keys.legacy_hmac_hex`). `consent/service.py` `verify_commitment` does exactly
that. Nothing in the product compares `ip_hash` across rows (it is not in the audit payload, the
hash chain, the research view, the detection features or any screen), so the change only matters
to a person comparing addresses by hand across the cutover date.

## Consequences

- Each use can be rotated or versioned without touching the others.
- `OTP_HMAC_KEY` itself is still the root: rotating it changes every derived key at once
  (`docs/RUNBOOK.md`, "Rotating secrets").
- Until the legacy invite check is removed, an invite token is checked under two keys. Both
  checks are limited to unused, unexpired invites for that email.
- Checking an old consent commitment or `ip_hash` needs the `OTP_HMAC_KEY` it was made with, so
  an operator who rotates it keeps a sealed copy of the old value.
