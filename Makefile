# Radix - one-command development stack.
#
# HydraDB runs in Docker (see docker-compose.yml); the FastAPI backend and the
# Vite frontend run as local processes so reloads stay instant.
#
#   make dev      HydraDB + seed + backend + frontend, one terminal
#   make help     every target

RADIX_ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# `.env` is optional and uses plain KEY=value lines, which make parses natively.
# Included *before* the defaults below so that a local override always wins.
-include $(RADIX_ROOT)/.env

PYTHON  ?= /opt/homebrew/bin/python3.12
COMPOSE ?= docker compose

HYDRA_ADMIN_URL  ?= http://127.0.0.1:9090
HYDRA_HTTP_URL   ?= http://127.0.0.1:8443
HYDRA_AUTH_TOKEN ?= local-development-token-32-bytes
HYDRA_NAMESPACE  ?= radix
HYDRA_GRAPH_ID   ?= default
HYDRA_CELL_ID    ?= cell-0

BACKEND_HOST  ?= 127.0.0.1
BACKEND_PORT  ?= 8000
FRONTEND_PORT ?= 5173

# Seconds `wait-ready` will poll /readyz before giving up.
READY_TIMEOUT ?= 90

BACKEND_DIR  := $(RADIX_ROOT)/backend
FRONTEND_DIR := $(RADIX_ROOT)/frontend
VENV         := $(BACKEND_DIR)/.venv
VENV_PY      := $(VENV)/bin/python
TOKEN_FILE   := $(RADIX_ROOT)/hydra/auth-token
SEEDER       := $(RADIX_ROOT)/scripts/seed_ecosystem.py

# The seeder and the backend read their HydraDB coordinates from the
# environment, so hand them down instead of duplicating them per target.
# HydraClient reads HYDRA_TOKEN specifically; alias it so a token override in
# .env reaches Python instead of silently diverging from the curl targets.
HYDRA_TOKEN ?= $(HYDRA_AUTH_TOKEN)
export HYDRA_HTTP_URL HYDRA_ADMIN_URL HYDRA_AUTH_TOKEN HYDRA_TOKEN
export HYDRA_NAMESPACE HYDRA_GRAPH_ID HYDRA_CELL_ID
# Optional; enables the auto-PR push path. Empty is fine - open-pr then runs
# in dry-run mode only.
GITHUB_TOKEN ?=
export GITHUB_TOKEN
export BACKEND_HOST BACKEND_PORT FRONTEND_PORT

# Namespace that holds real ingested data (the demo world stays in 'radix').
# A sub-scope of the boot namespace: the auth token is prefix-scoped, so with
# the dev container booted as 'radix', only 'radix' and 'radix/...' are
# authorized. Production boots its own namespace (see deploy/).
LIVE_NAMESPACE ?= radix/live

.PHONY: help up wait-ready seed backend frontend dev down clean verify \
        venv install install-backend install-frontend logs ps restart \
        ingest osv-sync sentinel live-backend check-claims

help: ## Show this help
	@printf '\nRadix - make targets\n\n'
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN { FS = ":.*## " } { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 }'
	@printf '\n'

# --- HydraDB ---------------------------------------------------------------

up: ## Start HydraDB and block until /readyz answers
	@test -f "$(TOKEN_FILE)" || { \
		printf 'missing %s - compose bind-mounts it and Docker would create a directory in its place\n' '$(TOKEN_FILE)' >&2; \
		exit 1; \
	}
	@$(COMPOSE) up -d --remove-orphans
	@$(MAKE) --no-print-directory wait-ready

wait-ready: ## Poll HydraDB /readyz until it answers, or fail after READY_TIMEOUT
	@printf 'waiting for HydraDB at %s/readyz ' '$(HYDRA_ADMIN_URL)'
	@for i in $$(seq 1 $(READY_TIMEOUT)); do \
		if curl -fsS --max-time 2 -o /dev/null "$(HYDRA_ADMIN_URL)/readyz" 2>/dev/null; then \
			printf ' ready after %ss\n' "$$i"; \
			exit 0; \
		fi; \
		printf '.'; \
		sleep 1; \
	done; \
	printf '\nHydraDB was not ready after %ss; last 40 log lines:\n' '$(READY_TIMEOUT)' >&2; \
	$(COMPOSE) logs --tail=40 hydra >&2; \
	exit 1

down: ## Stop HydraDB, keeping the graph data volumes
	@$(COMPOSE) down --remove-orphans

restart: ## Recreate the HydraDB container, keeping the graph data volumes
	@$(COMPOSE) up -d --force-recreate
	@$(MAKE) --no-print-directory wait-ready

logs: ## Tail HydraDB logs
	@$(COMPOSE) logs -f --tail=100 hydra

ps: ## Show container status
	@$(COMPOSE) ps

# --- Dependencies ----------------------------------------------------------

# System python3 on macOS is 3.9, which is too old for the backend's typing;
# PYTHON pins the 3.12 interpreter that creates the venv.
$(VENV_PY):
	@printf 'creating virtualenv at %s (%s)\n' '$(VENV)' "$$($(PYTHON) --version)"
	@$(PYTHON) -m venv "$(VENV)"
	@"$(VENV_PY)" -m pip install --quiet --upgrade pip

venv: $(VENV_PY) ## Create the backend virtualenv

install-backend: venv ## Install backend Python dependencies
	@if [ -f "$(BACKEND_DIR)/requirements.txt" ]; then \
		"$(VENV_PY)" -m pip install --quiet -r "$(BACKEND_DIR)/requirements.txt"; \
	else \
		printf 'no backend/requirements.txt yet - skipping\n'; \
	fi

install-frontend: ## Install frontend npm dependencies
	@if [ -f "$(FRONTEND_DIR)/package.json" ]; then \
		cd "$(FRONTEND_DIR)" && npm install --no-fund --no-audit; \
	else \
		printf 'no frontend/package.json yet - skipping\n'; \
	fi

install: install-backend install-frontend ## Install all dependencies

# --- Application -----------------------------------------------------------

seed: install-backend ## Seed the demo ecosystem graph into HydraDB
	@test -f "$(SEEDER)" || { printf 'missing %s\n' '$(SEEDER)' >&2; exit 1; }
	@$(MAKE) --no-print-directory wait-ready
	@"$(VENV_PY)" "$(SEEDER)"

backend: install-backend ## Run the FastAPI backend with autoreload
	@cd "$(BACKEND_DIR)" && "$(VENV)/bin/uvicorn" app.main:app \
		--host "$(BACKEND_HOST)" --port "$(BACKEND_PORT)" --reload

frontend: install-frontend ## Run the Vite dev server
	@cd "$(FRONTEND_DIR)" && npm run dev

dev: ## Everything: HydraDB, seed, then backend + frontend side by side
	@"$(RADIX_ROOT)/scripts/dev.sh"

# --- Real data -------------------------------------------------------------

ingest: install-backend ## Ingest real repos: make ingest TARGETS="path-or-url ..."
	@test -n "$(TARGETS)" || { printf 'usage: make ingest TARGETS="/path/to/repo https://github.com/org/repo"\n' >&2; exit 2; }
	@$(MAKE) --no-print-directory wait-ready
	@"$(VENV_PY)" "$(RADIX_ROOT)/scripts/ingest.py" --namespace "$(LIVE_NAMESPACE)" $(TARGETS)

osv-sync: install-backend ## One advisory sweep over the live namespace
	@HYDRA_NAMESPACE="$(LIVE_NAMESPACE)" "$(VENV_PY)" -m sentinel.watcher --once

sentinel: install-backend ## Run the 24/7 advisory watcher in the foreground
	@HYDRA_NAMESPACE="$(LIVE_NAMESPACE)" "$(VENV_PY)" -m sentinel.watcher

live-backend: install-backend ## Backend pointed at real data instead of the demo world
	@cd "$(BACKEND_DIR)" && HYDRA_NAMESPACE="$(LIVE_NAMESPACE)" "$(VENV)/bin/uvicorn" app.main:app \
		--host "$(BACKEND_HOST)" --port "$(BACKEND_PORT)" --reload

# --- Housekeeping ----------------------------------------------------------

clean: ## Stop HydraDB, drop its volumes, and remove local build artefacts
	@$(COMPOSE) down --volumes --remove-orphans
	@rm -rf "$(VENV)" "$(FRONTEND_DIR)/node_modules" "$(FRONTEND_DIR)/dist"
	@find "$(RADIX_ROOT)" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@printf 'clean\n'

check-claims: ## Verify every number quoted in the README against a running Radix
	@"$(PYTHON)" "$(RADIX_ROOT)/scripts/check_claims.py" $(if $(TARGET),--target "$(TARGET)",)

verify: ## Check the compose file, the auth token, and a live query round-trip
	@printf '\nRadix - stack verification\n\n'
	@$(COMPOSE) config --quiet
	@printf '  ok    docker-compose.yml parses\n'
	@token=$$(tr -d '\n' < "$(TOKEN_FILE)"); \
	if [ $${#token} -lt 32 ]; then \
		printf '  FAIL  hydra/auth-token is %s bytes; HydraDB requires >= 32\n' "$${#token}" >&2; \
		exit 1; \
	fi; \
	printf '  ok    hydra/auth-token is %s bytes\n' "$${#token}"; \
	if ! grep -qx "HYDRA_AUTH_TOKEN=$$token" "$(RADIX_ROOT)/.env.example"; then \
		printf '  FAIL  hydra/auth-token does not match HYDRA_AUTH_TOKEN in .env.example\n' >&2; \
		exit 1; \
	fi; \
	printf '  ok    token matches .env.example\n'
	@code=$$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$(HYDRA_ADMIN_URL)/readyz" || true); \
	if [ "$$code" != "200" ]; then \
		printf '  FAIL  %s/readyz returned %s - run `make up`\n' '$(HYDRA_ADMIN_URL)' "$$code" >&2; \
		exit 1; \
	fi; \
	printf '  ok    %s/readyz -> 200\n' '$(HYDRA_ADMIN_URL)'
	@body=$$(curl -s --max-time 5 -X POST "$(HYDRA_HTTP_URL)/v1/graphs/$(HYDRA_GRAPH_ID)/query" \
		-H 'Authorization: Bearer $(HYDRA_AUTH_TOKEN)' \
		-H 'X-Graph-Namespace: $(HYDRA_NAMESPACE)' \
		-H 'Content-Type: application/json' \
		-d '{"cell_id":"$(HYDRA_CELL_ID)","query":"MATCH (n:Package) RETURN count(*) AS packages"}'); \
	if [[ "$$body" == *'"error"'* ]]; then \
		printf '  FAIL  query rejected: %s\n' "$$body" >&2; \
		exit 1; \
	fi; \
	packages=$$(sed -n 's/.*"value":\([0-9][0-9]*\).*/\1/p' <<< "$$body"); \
	printf '  ok    authenticated query round-trip -> %s Package nodes\n' "$${packages:-0}"; \
	if [ "$${packages:-0}" -lt 2 ]; then \
		printf '\n  note  graph looks unseeded - run `make seed`\n'; \
	fi
	@printf '\n'
