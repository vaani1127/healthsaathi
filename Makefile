COMPOSE := docker compose -f infra/docker-compose.dev.yml
DEMO := docker compose -f infra/docker-compose.demo.yml
UV := uv run
PY_TYPED := packages/e2d-core/src apps/api sim/src sim/tests experiments/src experiments/tests \
	infra/demo/src infra/demo/tests

.PHONY: help env install demo demo-code demo-e2e fhir-validate demo-logs demo-down demo-reset dev vectors api-types e2e contracts-local contracts-local-stop contracts-amoy 	contracts-deploy tamper-demo dev-down db-up lint lint-py lint-js lint-sol format test test-py test-js \
	test-sol migrate seed sim sim-coverage sim-audit sim-splits exp prereg-audit prereg-stamp audit

help:
	@echo "env       create .env with fresh local secrets"
	@echo "install   install Python, JS and Solidity dependencies"
	@echo "demo      run the whole app locally in Docker with demo data, then print the logins"
	@echo "demo-code print the current authenticator code of a demo login, EMAIL=..."
	@echo "demo-e2e  Playwright suite against the running demo (flows, crawler, PWA, tamper)"
	@echo "fhir-validate  export FHIR bundles of 10 demo patients and run the HAPI validator"
	@echo "demo-down stop the demo (keeps its data); demo-reset deletes the demo data"
	@echo "dev       start postgres, api and web with docker compose"
	@echo "lint      ruff, mypy, eslint, tsc, forge fmt --check"
	@echo "test      pytest, vitest, forge test"
	@echo "migrate   alembic upgrade head"
	@echo "seed      load the synthetic demo clinics"
	@echo "tamper-demo  show a rewritten audit log failing verification (local)"
	@echo "sim       run the simulator, CONFIG=path/to/config.yaml (default sim/configs/v1.yaml)"
	@echo "sim-coverage  explanation coverage per role of a run, RUN=sim/output/<name>"
	@echo "sim-audit     separability audit of a run (must pass before experiments)"
	@echo "sim-splits    write the four evaluation splits of a run"
	@echo "exp       run an experiment, NAME=smoke|v1, ARGS=\"hydra overrides\""
	@echo "prereg-audit  audit the 10 registered v1 seeds before registration"
	@echo "prereg-stamp  OpenTimestamps proof of the registration commit (ARGS=--upgrade later)"

env:
	cd apps/api && $(UV) python -m app.scripts.gen_env

install:
	uv sync
	pnpm install
	git submodule update --init --recursive

dev: env
	$(COMPOSE) up --build

dev-down:
	$(COMPOSE) down

# Local demo (infra/docker-compose.demo.yml): postgres, anvil with the contracts, seed, api,
# worker and the production web build on http://localhost:8080.
demo:
	$(DEMO) up --build --detach --wait
	$(DEMO) exec -T api python -m hs_demo.logins

demo-code:
	@$(DEMO) exec -T api python -m hs_demo.logins $(EMAIL)

demo-logs:
	$(DEMO) logs --follow api worker

# Browser tests against the running demo (make demo first). Many sign-ins from one address in a
# minute, so the API runs without rate limits for the test and gets them back afterwards.
demo-e2e:
	DEMO_RATE_LIMIT=false $(DEMO) up --detach --wait api
	cd apps/web && pnpm exec playwright test -c playwright.demo.config.ts $(ARGS); 		status=$$?; cd ../.. && $(DEMO) up --detach --wait api; exit $$status

# FHIR export (P17): bundles of the 10 most active demo patients, checked by the HAPI validator.
fhir-validate:
	rm -rf .demo/fhir && mkdir -p .demo/fhir
	$(DEMO) exec -T api demo-entrypoint python -m hs_demo.fhir_samples /state/fhir 10
	$(DEMO) cp api:/state/fhir/. .demo/fhir
	sh infra/fhir/validate.sh .demo/fhir

demo-down:
	$(DEMO) down

demo-reset:
	$(DEMO) down --volumes

# CI provides its own postgres service, so it sets SKIP_DB_UP=1.
db-up:
ifndef SKIP_DB_UP
	$(COMPOSE) up -d --wait postgres
endif

lint: lint-py lint-js lint-sol

lint-py:
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(UV) mypy $(PY_TYPED)
	$(UV) lint-imports

lint-js:
	pnpm -r lint
	pnpm -r typecheck

lint-sol:
	cd contracts && forge fmt --check

format:
	$(UV) ruff check --fix .
	$(UV) ruff format .
	cd contracts && forge fmt

test: test-py test-js test-sol

test-py: db-up
	$(UV) pytest --cov=e2d_core --cov-report=term-missing:skip-covered --cov-fail-under=90

test-js:
	pnpm -r test

# Browser tests: starts the API and Vite itself, against the local database.
e2e: migrate
	cd apps/web && pnpm e2e && pnpm e2e:pwa

test-sol:
	cd contracts && forge test
	cd contracts && forge snapshot --check --no-match-test testFuzz

# Start anvil (if needed) and deploy the contracts to it.
contracts-local:
	sh contracts/script/deploy-local.sh

contracts-local-stop:
	-kill $$(cat contracts/.anvil.pid) && rm -f contracts/.anvil.pid

# Polygon Amoy. Needs AMOY_RPC_URL, DEPLOYER_PRIVATE_KEY and REGISTRY_ADMIN_1..3 in .env.
contracts-amoy:
	set -a && . ./.env && set +a && cd contracts && DEPLOY_NETWORK=amoy forge script 		script/Deploy.s.sol --rpc-url "$$AMOY_RPC_URL" --broadcast --private-key "$$DEPLOYER_PRIVATE_KEY"

# Any other EVM chain (for example the Besu test network): make contracts-deploy NETWORK=besu
contracts-deploy:
	set -a && . ./.env && set +a && cd contracts && DEPLOY_NETWORK=$(NETWORK) forge script 		script/Deploy.s.sol --rpc-url "$$CUSTOM_RPC_URL" --broadcast --private-key "$$DEPLOYER_PRIVATE_KEY"

# Local only: anchor a demo clinic on anvil, rewrite one audit row, and show verification fail.
tamper-demo: contracts-local
	cd apps/api && $(UV) python -m app.scripts.tamper_demo

# Regenerate the web app's typed API client from the API's OpenAPI document.
api-types:
	cd apps/api && $(UV) python -m app.scripts.export_openapi ../web/src/lib/api/openapi.json
	cd apps/web && pnpm exec openapi-typescript src/lib/api/openapi.json --default-non-nullable false -o src/lib/api/schema.d.ts

# Regenerate the ledger test vectors shared by e2d-core and receipt-verify.
vectors:
	$(UV) python -m e2d_core.ledger.vectors packages/receipt-verify/test/vectors/ledger.json

audit:
	uv export --frozen --no-dev --no-emit-workspace --no-hashes -o .pip-audit-requirements.txt
	$(UV) pip-audit --strict -r .pip-audit-requirements.txt
	pnpm audit --prod

migrate: db-up
	cd apps/api && $(UV) alembic upgrade head

seed: db-up
	cd apps/api && $(UV) python -m app.scripts.seed

# SaathiBench. CONFIG defaults to the full v1 run; output goes to sim/output/<name>.
CONFIG ?= sim/configs/v1.yaml
sim:
	$(UV) python -m saathibench.run $(CONFIG)

# Explanation coverage per role of a finished run.
RUN ?= sim/output/saathibench-v1
sim-coverage:
	$(UV) python -m saathibench.coverage $(RUN)

# Separability audit (SPEC 7); fails if any feature or a depth-2 tree separates attacks too well.
sim-audit:
	$(UV) python -m saathibench.audit $(RUN)

# The four evaluation splits of a run, written to $(RUN)/splits.json.
sim-splits:
	$(UV) python -m saathibench.splits $(RUN)

# Experiments (SPEC 8), configured with Hydra in experiments/configs. NAME picks the data config
# (smoke or v1); ARGS adds Hydra overrides, for example ARGS="ablation=no_forgery seeds=[0,1]".
NAME ?= smoke
exp:
	$(UV) python -m e2d_experiments.run data=$(NAME) $(ARGS)

# Before registration: simulate the 10 v1 seeds, run the separability audit and the realism
# measures, and write experiments/audit/v1/ (no method is run).
prereg-audit:
	$(UV) python -m e2d_experiments.prereg_audit

# After committing experiments/PREREGISTRATION.md: OpenTimestamps proof of that commit.
# Run again with ARGS=--upgrade a few hours later to complete it.
prereg-stamp:
	$(UV) python -m e2d_experiments.stamp $(ARGS)
