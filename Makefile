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
COMPOSE  := infra/docker-compose.yml
DEPLOY   := ./infra/deploy.sh
CHAOS    := ./scripts/chaos.sh
RUFF_CFG := $(CP_DIR)/pyproject.toml

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

.PHONY: help up down destroy logs ps status migrate seed demo smoke test \
        chaos-milvus chaos-minio chaos-postgres chaos-recover dashboard fmt lint

help: ## Show this help
	@echo "Milvus Control Plane — available targets:"
	@grep -E '^[a-z][a-zA-Z0-9_-]*:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Stack lifecycle  (WP-03)
# ---------------------------------------------------------------------------
up: ## Bring up the full stack (preflight, wait for health, migrate, seed)
	@echo "TODO (WP-03): $(DEPLOY) up --profile all"

down: ## Stop and remove containers, KEEP volumes
	@echo "TODO (WP-03): $(DEPLOY) down"

destroy: ## Stop and remove containers AND delete all volume data
	@echo "TODO (WP-03): $(DEPLOY) destroy"

logs: ## Tail logs for all services (or one: make logs s=milvus-standalone)
	@echo "TODO (WP-03): $(DEPLOY) logs $(s)"

ps: ## List stack containers
	@echo "TODO (WP-03): docker compose -f $(COMPOSE) ps"

status: ## Per-service health, endpoint probes and row counts
	@echo "TODO (WP-03): $(DEPLOY) status"

# ---------------------------------------------------------------------------
# Database  (WP-04) and seeding  (WP-03)
# ---------------------------------------------------------------------------
migrate: ## Apply Alembic migrations to head
	@echo "TODO (WP-04): alembic -c $(CP_DIR)/alembic.ini upgrade head"

seed: ## Register the local cluster in the control plane
	@echo "TODO (WP-03): ./scripts/seed_cluster.sh"

# ---------------------------------------------------------------------------
# Demo and verification  (WP-11, WP-14)
# ---------------------------------------------------------------------------
demo: ## Run the Milvus operations script end to end
	@echo "TODO (WP-11): python ops/milvus_demo.py --uri http://localhost:19530"

smoke: ## Walk every API endpoint and assert status codes and fields
	@echo "TODO (WP-14): ./scripts/smoke_test.sh"

test: ## Run the pytest suite
	@echo "TODO (WP-14): pytest $(CP_DIR)/app/tests"

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
	@echo "TODO (WP-12): npm --prefix dashboard run dev"

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
