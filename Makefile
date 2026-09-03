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
MCP_IMAGE ?= ai-ops-mcp:latest
RUNTIME_BUILD ?= docker build
SANDBOX_MODE ?= macos
COMPOSE_PROJECT_NAME ?= aiops-test
TEST_DOCKER_NETWORK ?= $(COMPOSE_PROJECT_NAME)-network
COMPOSE_BOOTSTRAP_ENV_FILE ?= compose.env
WORKFLOW_COMPOSE_ENV_FILE ?= $(shell sed -n 's/^WORKFLOW_COMPOSE_ENV_FILE=//p' "$(COMPOSE_BOOTSTRAP_ENV_FILE)" 2>/dev/null | tail -1)
WORKFLOW_COMPOSE_OVERRIDE_FILE ?= $(shell sed -n 's/^WORKFLOW_COMPOSE_OVERRIDE_FILE=//p' "$(COMPOSE_BOOTSTRAP_ENV_FILE)" 2>/dev/null | tail -1)
HOST_WORKFLOW_REPO_PATH ?= $(shell sed -n 's/^HOST_WORKFLOW_REPO_PATH=//p' "$(COMPOSE_BOOTSTRAP_ENV_FILE)" 2>/dev/null | tail -1)
HOST_PLATFORM_CONFIG_FILE ?= $(shell sed -n 's/^HOST_PLATFORM_CONFIG_FILE=//p' "$(COMPOSE_BOOTSTRAP_ENV_FILE)" 2>/dev/null | tail -1)
AGE_IDENTITY ?= $(shell sed -n 's/^AGE_IDENTITY=//p' "$(COMPOSE_BOOTSTRAP_ENV_FILE)" 2>/dev/null | tail -1)
GATEWAY_URL ?= http://localhost:8080
COMPOSE_ENV_FILES := $(if $(wildcard $(WORKFLOW_COMPOSE_ENV_FILE)),--env-file "$(WORKFLOW_COMPOSE_ENV_FILE)") $(if $(wildcard $(COMPOSE_BOOTSTRAP_ENV_FILE)),--env-file "$(COMPOSE_BOOTSTRAP_ENV_FILE)")
COMPOSE_FILES := -f deploy/docker-compose.yml $(if $(wildcard $(WORKFLOW_COMPOSE_OVERRIDE_FILE)),-f "$(WORKFLOW_COMPOSE_OVERRIDE_FILE)")
COMPOSE ?= docker compose $(COMPOSE_ENV_FILES) $(COMPOSE_FILES)
K8S_CHART ?= deploy/k8s/agentic-ops
K8S_BOOTSTRAP_SCRIPT ?= dist/bootstrap/k8s-secret.sh
K8S_RELEASE ?=
K8S_NAMESPACE ?=
K8S_VALUES_FILE ?=
K8S_PLATFORM_CONFIG_FILE ?=
K8S_PLATFORM_CONFIG_SECRET ?=
K8S_HELM_FLAGS ?= --wait --atomic
# Compose interpolates every service before starting the requested one. The test
# database only starts Postgres, so provide inert values for runtime-only secrets.
TEST_COMPOSE ?= AGE_IDENTITY=test-only-not-a-real-age-key LLM_API_KEY=test-only-not-a-real-llm-key PG_PORT=$(TEST_PG_PORT) DOCKER_NETWORK=$(TEST_DOCKER_NETWORK) docker compose --project-name $(COMPOSE_PROJECT_NAME) -f deploy/docker-compose.yml

.PHONY: help unit-tests service-tests runtime-tests tests \
	ensure-test-db up compose-build runtime-build mcp-build clean-test-containers \
	init bootstrap sync set-platform-secret set-workflow-secret k8s-bootstrap k8s-platform-config k8s-upgrade \
	k8s-deploy k8s-update

init: ## Install Python dependencies
	uv sync --extra dev

bootstrap: ## Guided operator bootstrap (workflow-repo pointer, AGE identity, model key)
	$(PYTHON) scripts/bootstrap.py

sync: ## Sync platform config and workflows through the running gateway
	@curl --fail --silent --show-error --request POST "$(GATEWAY_URL)/api/platform/workflow-repo/sync" | \
		$(PYTHON) -c 'import json, sys; payload = json.load(sys.stdin); status = payload.get("last_sync_status"); message = "Workflow sync completed." if status == "ok" else "Workflow sync failed: " + str(payload.get("last_sync_error") or "unknown error"); print(message, file=sys.stdout if status == "ok" else sys.stderr); raise SystemExit(status != "ok")'

set-platform-secret: ## Encrypt a secret in the configured platform-config.yaml
	@test -n "$(HOST_PLATFORM_CONFIG_FILE)" || (echo "HOST_PLATFORM_CONFIG_FILE is not configured; run make bootstrap." >&2; exit 1)
	@test -f "$(HOST_PLATFORM_CONFIG_FILE)" || (echo "Platform config not found: $(HOST_PLATFORM_CONFIG_FILE)" >&2; exit 1)
	@AGE_IDENTITY="$(AGE_IDENTITY)" $(PYTHON) scripts/set_secret.py \
		--scope shared \
		--platform-file "$(HOST_PLATFORM_CONFIG_FILE)"
	@$(MAKE) sync

set-workflow-secret: ## Encrypt a secret in WORKFLOW=<workflow>/agent.yaml
	@test -n "$(WORKFLOW)" || (echo "Set WORKFLOW, for example: make set-workflow-secret WORKFLOW=example-workflow" >&2; exit 1)
	@test -n "$(HOST_WORKFLOW_REPO_PATH)" || (echo "HOST_WORKFLOW_REPO_PATH is not configured; run make bootstrap." >&2; exit 1)
	@test -f "$(HOST_WORKFLOW_REPO_PATH)/workflows/$(WORKFLOW)/agent.yaml" || (echo "Workflow not found: $(HOST_WORKFLOW_REPO_PATH)/workflows/$(WORKFLOW)/agent.yaml" >&2; exit 1)
	@AGE_IDENTITY="$(AGE_IDENTITY)" $(PYTHON) scripts/set_secret.py \
		--scope plugin \
		--plugin "$(WORKFLOW)" \
		--plugins-dir "$(HOST_WORKFLOW_REPO_PATH)/workflows"
	@$(MAKE) sync

help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "%-28s %s\n", $$1, $$2}'

compose-build: ## Build all docker compose services
	@profiles="$$(HOST_PLATFORM_CONFIG_FILE="$(HOST_PLATFORM_CONFIG_FILE)" $(PYTHON) scripts/compose_profiles.py)"; \
	echo "Computed COMPOSE_PROFILES=$$profiles"; \
	COMPOSE_PROFILES="$$profiles" $(COMPOSE) build

runtime-build: ## Build the runtime container image
	$(RUNTIME_BUILD) -t $(RUNTIME_IMAGE) -f runtime/Dockerfile .

mcp-build: ## Build the shared MCP server container image used by runtime smoke tests
	docker build -t $(MCP_IMAGE) -f mcps/Dockerfile .

build: runtime-build compose-build ## Build all docker images

up: ## Start the local Compose stack with profiles derived from platform-config.yaml
	@profiles="$$(HOST_PLATFORM_CONFIG_FILE="$(HOST_PLATFORM_CONFIG_FILE)" $(PYTHON) scripts/compose_profiles.py)"; \
	echo "Computed COMPOSE_PROFILES=$$profiles"; \
	COMPOSE_PROFILES="$$profiles" $(COMPOSE) up -d

down: ## Stop the local docker compose stack
	$(COMPOSE) down

restart: down build up ## Restart the local docker compose stack

restart-%: ## Restart a specific service (e.g. `make restart-postgres` or `make restart-runtime`)
	$(COMPOSE) build $*
	$(COMPOSE) up -d $* --force-recreate

k8s-bootstrap: ## Apply the generated Kubernetes bootstrap Secret
	@test -n "$(K8S_NAMESPACE)" || (echo "Set K8S_NAMESPACE." >&2; exit 1)
	@test -x "$(K8S_BOOTSTRAP_SCRIPT)" || (echo "Missing executable $(K8S_BOOTSTRAP_SCRIPT); run make bootstrap for Kubernetes first." >&2; exit 1)
	@grep -F -- "--namespace $(K8S_NAMESPACE)" "$(K8S_BOOTSTRAP_SCRIPT)" >/dev/null || (echo "$(K8S_BOOTSTRAP_SCRIPT) does not target namespace $(K8S_NAMESPACE); rerun make bootstrap or use the matching namespace." >&2; exit 1)
	"$(K8S_BOOTSTRAP_SCRIPT)"

k8s-platform-config: ## Create/update the Secret containing platform-config.yaml
	@test -n "$(K8S_NAMESPACE)" || (echo "Set K8S_NAMESPACE." >&2; exit 1)
	@test -n "$(K8S_PLATFORM_CONFIG_SECRET)" || (echo "Set K8S_PLATFORM_CONFIG_SECRET." >&2; exit 1)
	@test -f "$(K8S_PLATFORM_CONFIG_FILE)" || (echo "Set K8S_PLATFORM_CONFIG_FILE to platform-config.yaml." >&2; exit 1)
	kubectl create secret generic "$(K8S_PLATFORM_CONFIG_SECRET)" \
		--namespace "$(K8S_NAMESPACE)" \
		--from-file=platform-config.yaml="$(K8S_PLATFORM_CONFIG_FILE)" \
		--dry-run=client -o yaml | kubectl apply -f -

k8s-upgrade: ## Apply the Helm release from an instance values file
	@test -n "$(K8S_RELEASE)" || (echo "Set K8S_RELEASE." >&2; exit 1)
	@test -n "$(K8S_NAMESPACE)" || (echo "Set K8S_NAMESPACE." >&2; exit 1)
	@test -f "$(K8S_VALUES_FILE)" || (echo "Set K8S_VALUES_FILE to the instance deploy/k8s-values.yaml." >&2; exit 1)
	helm upgrade --install "$(K8S_RELEASE)" "$(K8S_CHART)" \
		--namespace "$(K8S_NAMESPACE)" --create-namespace \
		--values "$(K8S_VALUES_FILE)" $(K8S_HELM_FLAGS)

k8s-deploy: ## Initial Kubernetes deployment: bootstrap Secret, config Secret, then Helm release
	@$(MAKE) k8s-bootstrap K8S_BOOTSTRAP_SCRIPT="$(K8S_BOOTSTRAP_SCRIPT)" K8S_NAMESPACE="$(K8S_NAMESPACE)"
	@$(MAKE) k8s-platform-config K8S_NAMESPACE="$(K8S_NAMESPACE)" K8S_PLATFORM_CONFIG_SECRET="$(K8S_PLATFORM_CONFIG_SECRET)" K8S_PLATFORM_CONFIG_FILE="$(K8S_PLATFORM_CONFIG_FILE)"
	@$(MAKE) k8s-upgrade K8S_RELEASE="$(K8S_RELEASE)" K8S_NAMESPACE="$(K8S_NAMESPACE)" K8S_VALUES_FILE="$(K8S_VALUES_FILE)" K8S_CHART="$(K8S_CHART)" K8S_HELM_FLAGS="$(K8S_HELM_FLAGS)"

k8s-update: ## Update Kubernetes config and Helm-managed services after a values or image-tag change
	@$(MAKE) k8s-platform-config K8S_NAMESPACE="$(K8S_NAMESPACE)" K8S_PLATFORM_CONFIG_SECRET="$(K8S_PLATFORM_CONFIG_SECRET)" K8S_PLATFORM_CONFIG_FILE="$(K8S_PLATFORM_CONFIG_FILE)"
	@$(MAKE) k8s-upgrade K8S_RELEASE="$(K8S_RELEASE)" K8S_NAMESPACE="$(K8S_NAMESPACE)" K8S_VALUES_FILE="$(K8S_VALUES_FILE)" K8S_CHART="$(K8S_CHART)" K8S_HELM_FLAGS="$(K8S_HELM_FLAGS)"


ensure-test-db: ## Create the dedicated Postgres test database if needed
	$(TEST_COMPOSE) up -d --wait postgres
	@$(TEST_COMPOSE) exec -T postgres psql -U $(PGUSER) -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$(TEST_DB_NAME)'" | grep -q 1 || \
		$(TEST_COMPOSE) exec -T postgres psql -U $(PGUSER) -d postgres -c "CREATE DATABASE $(TEST_DB_NAME)"

unit-tests: ## Run unit tests (no infra required)
	$(PYTEST) tests/unit $(PYTEST_FLAGS)

service-tests: ensure-test-db ## Run service/Postgres tests
	TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(PYTEST) tests/service $(PYTEST_FLAGS)

runtime-tests: ensure-test-db runtime-build mcp-build ## Run runtime scenario tests (Postgres + Docker required)
	TEST_DATABASE_URL='$(TEST_DATABASE_URL)' TEST_RUNTIME_ENABLED=1 SANDBOX_MODE=$(SANDBOX_MODE) DOCKER_NETWORK=$(TEST_DOCKER_NETWORK) \
		COMPOSE_PROJECT_NAME=$(COMPOSE_PROJECT_NAME) MCP_TEST_IMAGE=$(MCP_IMAGE) \
		$(PYTEST) tests/runtime $(PYTEST_TIMEOUT_FLAGS) $(PYTEST_FLAGS)

tests: ensure-test-db runtime-build mcp-build ## Run all tests, including unit, service, and runtime scenarios
	TEST_DATABASE_URL='$(TEST_DATABASE_URL)' TEST_RUNTIME_ENABLED=1 SANDBOX_MODE=$(SANDBOX_MODE) DOCKER_NETWORK=$(TEST_DOCKER_NETWORK) \
		COMPOSE_PROJECT_NAME=$(COMPOSE_PROJECT_NAME) MCP_TEST_IMAGE=$(MCP_IMAGE) \
		$(PYTEST) tests $(PYTEST_FLAGS)

clean-test-containers: ## Remove dangling test session containers
	-@docker ps -a --filter "label=agentic_ops.type=agent-session" --format "{{.ID}}" | xargs -r docker rm -f

format: ## Format code (ruff check + fix)
	uv run ruff check . --fix && uv run ruff format . && npm --prefix control-plane-ui run format

lint: ## Lint (ruff check + format check)
	uv run ruff check . && uv run ruff format --check . && npm --prefix control-plane-ui run lint && npm --prefix control-plane-ui run format:check