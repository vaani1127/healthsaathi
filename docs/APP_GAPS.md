# App gap audit

Audit of the app against SPEC sections 1 (roles, modules, the 9 flows), 4 (security) and 9
(frontend), done before the local demo work. Each row says what is missing, where, and how it is
fixed. Status is updated as fixes land.

Method:

- Every role's screens compared with SPEC 1.1 and 1.2.
- Every API path in the OpenAPI document compared with the calls made by `apps/web/src`.
- Every problem code raised by the API compared with the `errors.*` keys in both locale files.
- English and Hindi locale files compared key by key (they match).
- Grep for TODO, FIXME, NotImplemented, placeholder, mock, lorem and hard-coded demo values in
  `apps/api/app` and `apps/web/src`: nothing found apart from input `placeholder` attributes.

## Gaps in existing features

| # | Gap | Where | Fix | Status |
|---|---|---|---|---|
| 1 | Sessions list and revoke (SPEC 1.2 identity must-have) have an API but no screen. | `GET/DELETE /auth/sessions`; no web caller | Add a "My sessions" page reachable from the header for every signed-in user, with revoke. | fixed |
| 2 | Invited staff cannot set a password: the invite email links to `/invite`, which does not exist. | `POST /auth/invite/accept`; router has no `/invite` | Add the `/invite` page (email and token from the link, password with the policy rules). | fixed |
| 3 | No new-device email alert (SPEC 1.2 identity, end-sem must-have). | `identity/service.py` `_device_id` | When a sign-in creates a new device row for a user who already had one, send an email. | fixed |
| 4 | Care team assignments (SPEC 1.2 clinic ops) have an API but no screen. | `POST /care-team`, `GET /patients/{id}/care-team`, `POST /care-team/{id}/end` | Care team card on the doctor's patient page (doctor and admin can add and end). | open |
| 5 | Shifts (SPEC 1.2 clinic ops) have an API but no screen. | `GET/POST /shifts`, `POST /shifts/{id}/cancel` | Shifts tab on the admin page: list the week, add, cancel. | open |
| 6 | Conditions can be read but not added or resolved. | `POST /patients/{id}/conditions`, `PATCH /conditions/{id}` | Add and resolve conditions on the doctor's patient page. | open |
| 7 | A signed note or prescription cannot be amended, and old versions cannot be seen (SPEC 3.1 versioning). | `POST /notes/{id}/versions`, `GET /notes/{id}/history`, `POST /prescriptions/{id}/versions` | "Amend" and "Versions" actions in the doctor's history cards. | open |
| 8 | Referrals cannot be completed or cancelled. | `PATCH /referrals/{id}` | Referral list with complete and cancel on the doctor's patient page. | open |
| 9 | Allergies cannot be marked inactive. | `PATCH /allergies/{id}` | "Mark inactive" on the nurse's allergy list. | open |
| 10 | Appointments cannot be cancelled or marked no-show. | `PATCH /appointments/{id}` | Cancel and no-show actions in reception's list of today's appointments. | open |
| 11 | The break-glass emergency view is lost on page reload; the reopen endpoint is unused. | `GET /patients/{id}/emergency` | Keep the break-glass id for the session and reopen the view while it is valid. | open |
| 12 | No lab report print view (SPEC 9 lists prescription, lab report and receipt). | `apps/web/src/router.tsx` | Add a bilingual `/print/lab/$orderId` view linked from released results. | open |
| 13 | No web push opt-in (SPEC 9). The `push_subscriptions` table exists but nothing uses it. | API and web | Add VAPID keys to the env, subscribe and unsubscribe endpoints, an opt-in switch in the patient portal, and a push job sent when someone opens the patient's record. | open |
| 14 | 71 of the 86 problem codes raised by the API have no translation, so the UI shows the raw English server text, also in Hindi. | `apps/web/src/lib/format.ts` `errorMessage`, locale files | Translate every code in English and Hindi, fall back to a message per HTTP status (never the raw text), and add a test that fails when a new code has no translation. | fixed |
| 15 | The admin audit page shows no loading state, shows the raw `problem` text, and lists only anchors that exist, so a backend that is not configured is invisible. | `AdminHome.tsx` `AuditStatus`, `anchor/receipts.py` | API returns the state of every backend (configured or not, last result, link); the page shows each one, translated. | open |
| 16 | Flow 9 (audit verification page) has no Playwright test. | `apps/web/e2e` | Covered by the new end-to-end suite against the demo stack. | open |
| 17 | P17 FHIR export is not built. | `apps/api/app/fhir` missing | Build it (see P17 in docs/PROMPTS.md). | open |
| 18 | P18 documents are missing. | `docs/SECURITY_CHECKLIST.md`, `docs/THREAT_MODEL.md` | Write them; run ZAP baseline and axe against the local stack. | open |

## Missing local demo pieces

| # | Gap | Fix | Status |
|---|---|---|---|
| 19 | No one-command demo. `make dev` runs Vite in dev mode with no worker, no chain and no witness. | `make demo` with `infra/docker-compose.demo.yml`: postgres, migrate, anvil with automatic contract deployment, demo seed, api, worker, production web build behind Caddy. | open |
| 20 | Anchoring cannot target a local chain or a local witness folder. | `ANCHOR_CHAIN=anvil` and `WITNESS_DIR` settings (details below). | open |
| 21 | The demo seed has patients and staff but no activity, so alert queue, break-glass queue and access logs are empty. | Run the SaathiBench simulator (unchanged) for 2 clinics and 14 days and load its output into the product tables. | open |
| 22 | No way to read email codes in a local demo without reading container logs. | Dev mailbox API and page, only when `APP_ENV=local`. | open |
| 23 | No end-to-end tests against the full stack (receipt against anvil, crawler, Hindi, tamper demo). | New Playwright suite `apps/web/e2e-demo`. | open |

## Accepted as is

| Item | Reason |
|---|---|
| Platform admin has no screens. | SPEC 1.1 says "Team only". Clinics are created and signer keys registered with `app.scripts.register_clinic`; the demo seed creates its clinics the same way. |
| `POST /internal/anchor/run`, `/health`, `/ready` have no UI caller. | They are for the scheduler and the load balancer, not for people. |

## Design decision: local anchoring without a data model change

`anchor_receipts.backend` is limited to `amoy`, `ots`, `github` and `besu` by a check constraint
(SPEC 3.1). The demo does not add values:

- The local anvil chain is a self-hosted EVM chain, the same kind of backend as Besu, so its
  receipts are stored as `besu`. A new `ANCHOR_CHAIN=anvil` setting picks that chain and the
  audit page shows it as "Local anvil chain".
- The local witness folder is a git repository, the same kind of witness as GitHub, so its
  receipts are stored as `github` with the commit hash. A new `WITNESS_DIR` setting writes and
  commits checkpoints to that folder instead of pushing to GitHub; the audit page shows it as
  "Local witness folder".
- OpenTimestamps stays off locally and the audit page shows "Not configured".
