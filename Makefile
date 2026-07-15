PYTHON := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PYTEST := $(PYTHON) -m pytest
PYTEST_FLAGS ?= $(if $(CI),-q,-vv -ra)
PYTEST_TIMEOUT_FLAGS ?=

PGUSER ?= agentic_ops
PGPASSWORD ?= localdev-postgres-password
TEST_DB_NAME ?= agentic_ops_test
TEST_PG_PORT ?= 55432
TEST_DATABASE_URL ?= postgresql+asyncpg://$(PGUSER):$(PGPASSWORD)@localhost:$(TEST_PG_PORT)/$(TEST_DB_NAME)
RUNTIME_IMAGE ?= ai-ops-agent-runtime:latest
RUNTIME_BUILD ?= docker build
COMPOSE_PROJECT_NAME ?= aiops-test
COMPOSE_BOOTSTRAP_ENV_FILE ?= compose.env
WORKFLOW_COMPOSE_ENV_FILE ?= $(shell sed -n 's/^WORKFLOW_COMPOSE_ENV_FILE=//p' "$(COMPOSE_BOOTSTRAP_ENV_FILE)" 2>/dev/null | tail -1)
HOST_PLATFORM_CONFIG_FILE ?= $(shell sed -n 's/^HOST_PLATFORM_CONFIG_FILE=//p' "$(COMPOSE_BOOTSTRAP_ENV_FILE)" 2>/dev/null | tail -1)
COMPOSE_ENV_FILES := $(if $(wildcard $(WORKFLOW_COMPOSE_ENV_FILE)),--env-file "$(WORKFLOW_COMPOSE_ENV_FILE)") $(if $(wildcard $(COMPOSE_BOOTSTRAP_ENV_FILE)),--env-file "$(COMPOSE_BOOTSTRAP_ENV_FILE)")
COMPOSE ?= docker compose $(COMPOSE_ENV_FILES) -f deploy/docker-compose.yml
# Compose interpolates every service before starting the requested one. The test
# database only starts Postgres, so provide inert values for runtime-only secrets.
TEST_COMPOSE ?= AGE_IDENTITY=test-only-not-a-real-age-key LLM_API_KEY=test-only-not-a-real-llm-key PG_PORT=$(TEST_PG_PORT) docker compose --project-name $(COMPOSE_PROJECT_NAME) -f deploy/docker-compose.yml

.PHONY: help unit-tests service-tests runtime-tests test \
	ensure-test-db up up-auto compose-build runtime-build clean-test-containers \
	init bootstrap set-secret

init: ## Install Python dependencies
	uv sync --extra dev

bootstrap: ## Guided operator bootstrap (workflow-repo pointer, AGE identity, model key)
	$(PYTHON) scripts/bootstrap.py

set-secret: ## Interactively encrypt and store a platform or agent secret
	$(PYTHON) scripts/set_secret.py

help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "%-28s %s\n", $$1, $$2}'

compose-build: ## Build all docker compose services
	$(COMPOSE) build

runtime-build: ## Build the runtime container image
	$(RUNTIME_BUILD) -t $(RUNTIME_IMAGE) -f runtime/Dockerfile .

build: runtime-build compose-build ## Build all docker images

up: ## Start the local docker compose stack
	$(COMPOSE) up -d

up-auto: ## Start only the services this instance's platform-config.yaml enables (profiles derived automatically)
	@profiles="$$(HOST_PLATFORM_CONFIG_FILE="$(HOST_PLATFORM_CONFIG_FILE)" $(PYTHON) scripts/compose_profiles.py)"; \
	echo "Computed COMPOSE_PROFILES=$$profiles"; \
	COMPOSE_PROFILES="$$profiles" $(COMPOSE) up -d

down: ## Stop the local docker compose stack
	$(COMPOSE) down

restart: down build up ## Restart the local docker compose stack

restart-%: ## Restart a specific service (e.g. `make restart-postgres` or `make restart-runtime`)
	$(COMPOSE) build $*
	$(COMPOSE) up -d $* --force-recreate


ensure-test-db: ## Create the dedicated Postgres test database if needed
	$(TEST_COMPOSE) up -d --wait postgres
	@$(TEST_COMPOSE) exec -T postgres psql -U $(PGUSER) -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$(TEST_DB_NAME)'" | grep -q 1 || \
		$(TEST_COMPOSE) exec -T postgres psql -U $(PGUSER) -d postgres -c "CREATE DATABASE $(TEST_DB_NAME)"

unit-tests: ## Run unit tests (no infra required)
	$(PYTEST) tests/unit $(PYTEST_FLAGS)

service-tests: ensure-test-db ## Run service/Postgres tests
	TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(PYTEST) tests/service $(PYTEST_FLAGS)

runtime-tests: ensure-test-db runtime-build ## Run runtime scenario tests (Postgres + Docker required)
	TEST_DATABASE_URL='$(TEST_DATABASE_URL)' TEST_RUNTIME_ENABLED=1 \
		COMPOSE_PROJECT_NAME=$(COMPOSE_PROJECT_NAME) \
		$(PYTEST) tests/runtime $(PYTEST_TIMEOUT_FLAGS) $(PYTEST_FLAGS)

test: ensure-test-db runtime-build ## Run all three suites (unit + service + runtime)
	TEST_DATABASE_URL='$(TEST_DATABASE_URL)' TEST_RUNTIME_ENABLED=1 \
		COMPOSE_PROJECT_NAME=$(COMPOSE_PROJECT_NAME) \
		$(PYTEST) tests $(PYTEST_FLAGS)

clean-test-containers: ## Remove dangling test session containers
	-@docker ps -a --filter "label=agentic_ops.type=agent-session" --format "{{.ID}}" | xargs -r docker rm -f

format: ## Format code (ruff check + fix)
	uv run ruff check . --fix && uv run ruff format . && npm --prefix control-plane-ui run format

lint: ## Lint (ruff check + format check)
	uv run ruff check . && uv run ruff format --check . && npm --prefix control-plane-ui run lint && npm --prefix control-plane-ui run format:check