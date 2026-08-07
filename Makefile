# Milvus 2.6 Control Plane — task runner.
#
# Target names are a public contract: the README, the demo script and
# docs/RELIABILITY.md all refer to them. Do not rename them.
#
# At WP-01 every target except fmt/lint is a stub. Each stub names the work
# package that implements it.

SHELL := /bin/bash
.DEFAULT_GOAL := help

CP_DIR   := control_plane
DEPLOY   := ./infra/deploy.sh
CHAOS    := ./scripts/chaos.sh
RUFF_CFG := $(CP_DIR)/pyproject.toml

# --project-directory pins the project root so relative bind mounts resolve to
# ./volumes and .env is actually read. Without it Compose treats infra/ as the
# project directory, silently drops every ${VAR}, and writes volume data to
# infra/volumes/. Always invoke Compose through this variable.
COMPOSE  := docker compose --env-file .env -f infra/docker-compose.yml --project-directory .
PROFILES := --profile infra --profile app

# Alembic runs on the HOST, so it needs the published port and localhost --
# .env holds cp-postgres:5432, which only resolves inside cp-net. Read the
# published port straight out of .env rather than `include`-ing the whole file,
# which would let any value in there become a Make variable.
PG_HOST_PORT := $(shell grep -E '^POSTGRES_HOST_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2)
PG_HOST_PORT := $(if $(PG_HOST_PORT),$(PG_HOST_PORT),5432)

# Prefer the project virtualenv; fall back to whatever is on PATH.
ALEMBIC := $(shell [ -x $(CP_DIR)/.venv/bin/alembic ] && echo ./.venv/bin/alembic || echo alembic)
# Same pattern: prefer the project venv, fall back to whatever is on PATH.
# Both are run after `cd $(CP_DIR)`, hence the ./ prefix.
PYTEST  := $(shell [ -x $(CP_DIR)/.venv/bin/pytest ] && echo ./.venv/bin/pytest || echo pytest)
# ops/ is standalone, but the project venv already carries pymilvus and numpy,
# so `make demo` reuses it rather than requiring a second environment. Run from
# the repo root, hence no ./ prefix.
DEMO_PY   := $(shell [ -x $(CP_DIR)/.venv/bin/python ] && echo $(CP_DIR)/.venv/bin/python || echo python3)
DEMO_ROWS ?= 5000
# --keep leaves the collection behind: the dashboard's collections panel and
# WP-14's integration tests both need populated data to look at.
DEMO_JSON ?= demo_results.json

# Prefer a ruff on PATH; otherwise run it through uv's ephemeral runner so a
# fresh clone can lint without a manual install step.
RUFF ?= $(shell command -v ruff 2>/dev/null || { command -v uvx >/dev/null 2>&1 && echo "uvx ruff"; })

define require_ruff
	@if [ -z "$(RUFF)" ]; then \
		echo "error: ruff not found."; \
		echo "  install it with:  pipx install ruff"; \
		echo "  or:               pip install -e '$(CP_DIR)[dev]'"; \
		exit 1; \
	fi
endef

.PHONY: help up down destroy logs ps status migrate migrate-down venv seed demo smoke test \
        chaos-milvus chaos-minio chaos-postgres chaos-recover dashboard fmt lint

help: ## Show this help
	@echo "Milvus Control Plane — available targets:"
	@grep -E '^[a-z][a-zA-Z0-9_-]*:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Stack lifecycle  (WP-03)
# ---------------------------------------------------------------------------
up: ## Bring up the full stack (preflight, wait for health, migrate, seed)
	$(DEPLOY) up

down: ## Stop and remove containers, KEEP volumes
	$(DEPLOY) down

destroy: ## Stop and remove containers AND delete all volume data (prompts)
	$(DEPLOY) destroy

logs: ## Tail logs for all services (or one: make logs s=milvus-standalone)
	$(DEPLOY) logs $(s)

ps: ## List stack containers
	$(COMPOSE) $(PROFILES) ps

status: ## Per-service health, endpoint probes and row counts
	$(DEPLOY) status

# ---------------------------------------------------------------------------
# Database  (WP-04) and seeding  (WP-03)
# ---------------------------------------------------------------------------
migrate: ## Apply Alembic migrations to head
	cd $(CP_DIR) && POSTGRES_HOST=localhost POSTGRES_PORT=$(PG_HOST_PORT) $(ALEMBIC) upgrade head

migrate-down: ## Roll all migrations back (destroys the control-plane schema)
	cd $(CP_DIR) && POSTGRES_HOST=localhost POSTGRES_PORT=$(PG_HOST_PORT) $(ALEMBIC) downgrade base

venv: ## Create the Python 3.12 virtualenv and install dependencies
	uv venv --python 3.12 $(CP_DIR)/.venv
	uv pip install --python $(CP_DIR)/.venv/bin/python -e '$(CP_DIR)[dev]'

seed: ## Register the local cluster in the control plane
	@echo "TODO (WP-03): ./scripts/seed_cluster.sh"

# ---------------------------------------------------------------------------
# Demo and verification  (WP-11, WP-14)
# ---------------------------------------------------------------------------
demo: ## Run the Milvus operations script end to end
	$(DEMO_PY) ops/milvus_demo.py --uri http://localhost:19530 \
		--rows $(DEMO_ROWS) --drop-existing --keep --json-out $(DEMO_JSON)

smoke: ## Walk every API endpoint and assert status codes and fields
	./scripts/smoke_test.sh

test: ## Run the pytest suite
	cd $(CP_DIR) && $(PYTEST) app/tests

# ---------------------------------------------------------------------------
# Reliability drills  (WP-15)
# ---------------------------------------------------------------------------
chaos-milvus: ## Inject: stop Milvus
	@echo "TODO (WP-15): $(CHAOS) milvus-stop"

chaos-minio: ## Inject: stop MinIO (shallow health stays green — the interesting one)
	@echo "TODO (WP-15): $(CHAOS) minio-stop"

chaos-postgres: ## Inject: stop Postgres (API must stay up and self-heal)
	@echo "TODO (WP-15): $(CHAOS) postgres-stop"

chaos-recover: ## Restore every injected failure
	@echo "TODO (WP-15): $(CHAOS) recover-all"

# ---------------------------------------------------------------------------
# Dashboard  (WP-12)
# ---------------------------------------------------------------------------
dashboard: ## Run the dashboard dev server against a live API
	npm --prefix dashboard install
	npm --prefix dashboard run dev

dashboard-build: ## Type-check and build the dashboard bundle
	npm --prefix dashboard install
	npm --prefix dashboard run build

dashboard-test: ## Run the dashboard render tests
	npm --prefix dashboard install
	npm --prefix dashboard test

# ---------------------------------------------------------------------------
# Code quality — implemented now
# ---------------------------------------------------------------------------
fmt: ## Format and auto-fix Python sources
	$(require_ruff)
	$(RUFF) format --config $(RUFF_CFG) $(CP_DIR) ops
	$(RUFF) check --config $(RUFF_CFG) --fix $(CP_DIR) ops

lint: ## Check formatting and lint rules without modifying files
	$(require_ruff)
	$(RUFF) check --config $(RUFF_CFG) $(CP_DIR) ops
	$(RUFF) format --config $(RUFF_CFG) --check $(CP_DIR) ops
