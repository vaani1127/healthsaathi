COMPOSE := docker compose -f infra/docker-compose.dev.yml
UV := uv run
PY_TYPED := packages/e2d-core/src apps/api

.PHONY: help env install dev dev-down db-up lint lint-py lint-js lint-sol format test test-py test-js \
	test-sol migrate seed sim exp audit

help:
	@echo "env       create .env with fresh local secrets"
	@echo "install   install Python, JS and Solidity dependencies"
	@echo "dev       start postgres, api and web with docker compose"
	@echo "lint      ruff, mypy, eslint, tsc, forge fmt --check"
	@echo "test      pytest, vitest, forge test"
	@echo "migrate   alembic upgrade head"
	@echo "seed      load the synthetic demo clinics"
	@echo "sim       run the simulator, CONFIG=path/to/config.yaml"
	@echo "exp       run an experiment, NAME=experiment_name"

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
	$(UV) pytest

test-js:
	pnpm -r test

test-sol:
	cd contracts && forge test

audit:
	uv export --frozen --no-dev --no-emit-workspace --no-hashes -o .pip-audit-requirements.txt
	$(UV) pip-audit --strict -r .pip-audit-requirements.txt
	pnpm audit --prod

migrate: db-up
	cd apps/api && $(UV) alembic upgrade head

seed: db-up
	cd apps/api && $(UV) python -m app.scripts.seed

sim:
	@echo "the simulator is added in phase P12" && exit 1

exp:
	@echo "experiments are added in phase P15" && exit 1
