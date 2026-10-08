COMPOSE := docker compose -f infra/docker-compose.dev.yml
UV := uv run
PY_TYPED := packages/e2d-core/src apps/api sim/src sim/tests experiments/src experiments/tests

.PHONY: help env install dev vectors api-types e2e contracts-local contracts-local-stop contracts-amoy 	contracts-deploy tamper-demo dev-down db-up lint lint-py lint-js lint-sol format test test-py test-js \
	test-sol migrate seed sim sim-coverage sim-audit sim-splits exp audit

help:
	@echo "env       create .env with fresh local secrets"
	@echo "install   install Python, JS and Solidity dependencies"
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

# Experiments (SPEC 8), configured with Hydra in experiments/conf. NAME picks the data config
# (smoke or v1); ARGS adds Hydra overrides, for example ARGS="ablation=no_forgery seeds=[0,1]".
NAME ?= smoke
exp:
	$(UV) python -m e2d_experiments.run data=$(NAME) $(ARGS)
