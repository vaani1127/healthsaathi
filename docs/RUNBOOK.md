# Runbook

How to deploy, roll back, rotate keys, restore a backup and keep an eye on free-tier usage. The
accounts and resources themselves are listed in `docs/MANUAL_TASKS.md` ("Before P11").

## Environments

| Environment | API | Database | Web | Deploys |
|---|---|---|---|---|
| local | `make dev` | Postgres in Docker | Vite dev server | by hand |
| staging | Render free web service | Neon free | Cloudflare Pages preview (`staging` branch) | after CI passes on `main` |
| prod-demo | Azure VM, Docker Compose with Caddy | Azure PostgreSQL Flexible B1MS | Cloudflare Pages production | on a `v*` tag |

Workflows: `.github/workflows/deploy.yml`, `anchor.yml` (every 15 minutes), `nightly.yml`
(backup and schema drift check). Each step is skipped while its secrets are not set.

## GitHub settings

Repository secrets:

| Name | Used by | Value |
|---|---|---|
| `STAGING_MIGRATOR_DATABASE_URL` | deploy, nightly | Neon owner URL, `postgresql+asyncpg://...?ssl=require` |
| `RENDER_DEPLOY_HOOK_URL` | deploy | Render service, Settings, Deploy Hook |
| `CLOUDFLARE_API_TOKEN` | deploy | token with "Cloudflare Pages: Edit" |
| `CLOUDFLARE_ACCOUNT_ID` | deploy | Cloudflare dashboard, account home |
| `VM_HOST`, `VM_USER` | deploy, nightly | VM public IP and SSH user |
| `VM_SSH_KEY` | deploy, nightly | private key of a deploy-only SSH key pair |
| `VM_KNOWN_HOSTS` | deploy, nightly | output of `ssh-keyscan -t ed25519 <VM_HOST>` |
| `BACKUP_PASSPHRASE` | nightly | long random passphrase, also kept offline by two members |
| `BACKUP_REPO_TOKEN` | nightly | fine-grained token, write access to the backup repo only |
| `ANCHOR_API_URL` | anchor | `https://api.<domain>` |
| `ANCHOR_TRIGGER_TOKEN` | anchor | same value as in the VM `.env` |

Repository variables (public values): `CF_PAGES_PROJECT`, `STAGING_API_URL`, `PROD_API_URL`,
`BACKUP_REPO` (`owner/healthsaathi-backups`), `VITE_CHAIN_RPC_URL`, `VITE_AUDIT_ANCHOR_ADDRESS`,
`VITE_CLINIC_REGISTRY_ADDRESS`, `VITE_EXPLORER_URL`.

Create two GitHub environments, `staging` and `production`. Adding yourself as a required reviewer
on `production` makes every prod deploy wait for a click.

## Database set-up (once per database)

The first migration creates the `NOLOGIN` roles `migrator`, `app_rw`, `anchor_job` and
`researcher_ro` (ADR 0002). Run migrations as the server admin login, then create a separate login
for the app that can only switch into `app_rw` and `anchor_job`:

```sql
CREATE ROLE hs_app LOGIN PASSWORD '<long random password>';
GRANT app_rw, anchor_job TO hs_app WITH INHERIT FALSE;
GRANT CONNECT ON DATABASE healthsaathi TO hs_app;
```

`WITH INHERIT FALSE` means `hs_app` has no rights of its own: every query must go through the
app's `SET LOCAL ROLE`, so row level security always applies. Then:

- `MIGRATOR_DATABASE_URL`: the admin login.
- `DATABASE_URL`: `hs_app`.
- `BACKUP_DATABASE_URL` (VM only): the admin login in plain `postgresql://` form.

Azure and Neon need TLS: add `?ssl=require` to the asyncpg URLs and `?sslmode=require` to the
backup URL.

## First prod-demo deploy

On the VM (Ubuntu 24.04):

1. Install Docker Engine and the compose plugin from Docker's apt repository. Add the deploy user
   to the `docker` group.
2. `sudo mkdir -p /opt/healthsaathi && sudo chown <deploy user> /opt/healthsaathi`
3. Create `/opt/healthsaathi/.env` from `.env.example` with production values: `APP_ENV=prod`,
   the database URLs above, fresh secrets (`make env` on a laptop prints a set, copy only the
   secret lines), `API_DOMAIN=api.<domain>`, `COOKIE_DOMAIN=.<domain>`,
   `CORS_ORIGINS=["https://app.<domain>"]`, `PUBLIC_APP_URL=https://app.<domain>`, the Brevo, blob
   and anchoring values. `chmod 600` the file.
4. Point `api.<domain>` (A record) at the VM IP and `app.<domain>` (CNAME) at the Pages project
   before tagging, so Caddy can get its certificate.
5. Tag and push: `git tag v0.1.0 && git push origin v0.1.0`.

The workflow checks that the tagged commit is on `main` and passed CI, builds the image, copies it
and the compose files to `/opt/healthsaathi`, and runs `infra/deploy/deploy.sh`, which:

1. loads the image,
2. runs `alembic upgrade head` as the migrator,
3. starts the new `api`, `worker` and `caddy`,
4. polls `https://api.<domain>/api/v1/ready` for up to two minutes,
5. on failure starts the previous tag again and fails the job.

After the first deploy, register each clinic on chain (see "Anchoring" below).

## Staging

Push to `main`. When CI passes, the workflow migrates Neon, calls the Render deploy hook and
publishes the web app to the Pages `staging` branch. The first time, create the Render service
from `render.yaml` (Blueprint) and fill in its secret values in the Render dashboard.

Known limit: staging runs on `onrender.com` and `pages.dev`, which are different sites, so the
browser does not send the refresh cookie and a page reload signs you out. Prod-demo uses one
domain for `app.` and `api.`, so the cookie works there.

The free Render service sleeps after 15 minutes without traffic; the first request after that
takes up to a minute.

## Rollback

- API: on the VM, `cat /opt/healthsaathi/current_tag` shows the running tag, and
  `docker image ls healthsaathi-api` the previous one, which is always kept. Run
  `HS_TAG=<previous> docker compose -f /opt/healthsaathi/docker-compose.prod.yml up -d api worker`
  and write the tag into `current_tag`. Or re-run the deploy workflow for the older tag.
- Migrations: write them so the previous release still works on the new schema (add columns and
  tables first, remove them in a later release). Never edit an applied revision. If a migration
  must be undone, add a new revision that undoes it.
- Web: in the Cloudflare Pages dashboard, open Deployments and choose "Rollback" on an earlier one.

## Rotating secrets

| Secret | How | Effect |
|---|---|---|
| `JWT_SECRET` | Replace in `.env`, restart `api` and `worker` | Access tokens in use stop working; the web app refreshes them silently |
| `DATA_KEYS` | Add the new key FIRST in the list and keep the old ones, restart | New data uses the new key; old data still decrypts. Never remove a key that encrypted stored data |
| `OTP_HMAC_KEY` | Replace, restart | Email codes sent before the change stop working |
| `ANCHOR_TRIGGER_TOKEN` | Replace in the VM `.env` and the GitHub secret together | Scheduled runs fail until both match; the worker fallback keeps anchoring |
| `ANCHOR_POSTER_PRIVATE_KEY` | New wallet, fund it, then two registry admins re-register the poster | Anchoring stops until the new poster is registered |
| `LEDGER_SIGNING_SEED` | Do not change | Checkpoints are refused if the derived key does not match the key stored for the clinic. Rotating it needs `ClinicRegistry.rotateKey` and app support that is not built yet |
| Database passwords | `ALTER ROLE ... PASSWORD`, update `.env`, restart | Short reconnect |
| `VM_SSH_KEY` | New key pair, add the public key on the VM, update the secret, remove the old key | None |

## Anchoring

- Status: Clinic admin, "Audit log" tab, or `GET /api/v1/audit/status`.
- Manual run: Actions, "anchor", "Run workflow"; or on the VM
  `docker compose -f /opt/healthsaathi/docker-compose.prod.yml exec api python -m app.anchor.job`.
- The worker runs the job itself if no checkpoint or anchor attempt happened for
  `ANCHOR_FALLBACK_MINUTES`.
- Registering a clinic: two of the three registry admins each run
  `python -m app.scripts.register_clinic <clinic-id> --send` with their own key in
  `REGISTRY_ADMIN_PRIVATE_KEY` (in the shell, never in a file), or use the printed `cast send`
  command.
- A critical log line "audit log does not match the anchored root" means the database history no
  longer matches what was anchored. Stop and investigate; do not try to re-anchor.
- Keep the poster wallet funded with test POL from the faucets.

## Detection

- The worker scores every clinic's accesses each `DETECT_INTERVAL_SECONDS` and refits models
  once a day (models in the `models` volume, `MODEL_DIR`). Before the first fit, and for clinics
  with few accesses, the rules baseline or the global model is used.
- Run by hand on the VM:
  `docker compose -f /opt/healthsaathi/docker-compose.prod.yml exec worker python -m app.detect.job`
  (add `--fit` to refit now).
- Every alert stores `model_version` (sha256 of the model file) and `policy_version`.

## Backups and restore

`nightly.yml` runs `infra/deploy/backup.sh` on the VM over SSH, encrypts the dump with
`BACKUP_PASSPHRASE` on the runner and commits it to the private backup repository, keeping the
last 14. The plain dump only travels inside the SSH connection, is encrypted as it arrives and is
never written to disk.

Restore (test this once a month on a scratch database):

```sh
gpg --decrypt healthsaathi-YYYYMMDD.dump.gpg > restore.dump
createdb -h <host> -U <admin> healthsaathi_restore
pg_restore -h <host> -U <admin> -d healthsaathi_restore --no-owner restore.dump
```

The roles are cluster-wide and are not in the dump; on a fresh server create them first by running
`alembic upgrade head` on an empty database, or with the `CREATE ROLE` statements from the first
migration. Delete `restore.dump` afterwards.

## Free-tier checks (weekly)

- Azure: Cost Management, "Cost analysis" for `rg-healthsaathi`. Expected cost is 0 under Azure for
  Students credits. The USD 1 budget alert must exist. Check the VM size is still B2ats v2 or B1s
  and the database is B1MS with high availability off.
- Neon: project dashboard, storage and compute hours.
- Render: workspace usage, free instance hours.
- Cloudflare Pages: builds per month (deploys come from GitHub Actions, not Pages builds).
- Brevo: daily email count against the free limit.
- GitHub Actions: Settings, Billing, minutes used (the repo is public, so standard runners are
  free).
- Amoy: poster wallet balance on the explorer.

If anything shows a charge, stop the resource first and then find out why.
