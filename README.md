# HealthSaathi

OPD clinic management for small Indian clinics, installable as a web app, in English and Hindi.

Every staff access to a patient record is explained from clinic workflow evidence (appointment,
queue token, lab order, referral, care team, break-glass). Accesses that cannot be explained are
ranked for an admin to review. Every access is written to an append-only log whose signed
checkpoints are anchored on public witnesses, and patients can verify their own access receipts in
the browser.

## Layout

| Path | What |
|---|---|
| `apps/web` | React PWA |
| `apps/api` | FastAPI app |
| `packages/e2d-core` | Explanation engine, features, scoring, Merkle ledger (Python) |
| `packages/receipt-verify` | Receipt verification (TypeScript) |
| `contracts` | Solidity contracts (Foundry) |
| `sim` | SaathiBench simulator |
| `experiments` | Experiment configs, runners and statistics |
| `infra` | Docker compose files |
| `docs` | Changelog and architecture decisions |

## Getting started

Requirements: Docker, Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22 with pnpm,
[Foundry](https://getfoundry.sh/) and GNU Make.

```sh
git clone --recurse-submodules https://github.com/vaani1127/healthsaathi.git
cd healthsaathi
cp .env.example .env
make install
make dev      # postgres, api on :8000, web on :5173
make lint
make test
```

## Compliance

Designed to support obligations under the DPDP Act 2023 and DPDP Rules 2025. Not legally reviewed
or certified. All data in this repository is synthetic.
