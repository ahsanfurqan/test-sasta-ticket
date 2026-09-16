# Everything runs in docker compose. Nothing here assumes a local python.
COMPOSE := docker compose
RUN     := $(COMPOSE) run --rm --no-deps

.PHONY: help up down migrate test lint load-test logs ps psql redis-cli shell fmt clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.env:
	@cp .env.example .env && echo "created .env from .env.example"

up: .env ## Build and start the stack, run migrations, wait for health
	$(COMPOSE) up -d --build
	@echo "waiting for api to report healthy..."
	@for i in $$(seq 1 40); do \
		status=$$($(COMPOSE) ps api --format '{{.Health}}' 2>/dev/null); \
		if [ "$$status" = "healthy" ]; then echo "api healthy"; break; fi; \
		if [ $$i -eq 40 ]; then echo "api did not become healthy"; $(COMPOSE) logs api; exit 1; fi; \
		sleep 1; \
	done
	@$(MAKE) migrate
	@echo ""
	@echo "  api      http://localhost:8000"
	@echo "  docs     http://localhost:8000/docs"
	@echo "  echo     curl -H \"X-API-Key: $$(grep DEV_API_KEY .env | cut -d= -f2)\" 'http://localhost:8000/v1/echo?message=hi'"

down: ## Stop and remove containers (VOLUMES=1 also drops data)
	@if [ "$(VOLUMES)" = "1" ]; then $(COMPOSE) down -v; else $(COMPOSE) down; fi

migrate: ## Apply migrations (alembic upgrade head)
	$(COMPOSE) exec -T api alembic upgrade head

revision: ## Draft a migration: make revision m="add customers"
	$(COMPOSE) exec -T api alembic revision --autogenerate -m "$(m)"

test: ## Run the test suite inside the api container
	$(COMPOSE) exec -T api pytest

lint: ## ruff + import-linter (enforces the meter.domain purity seam)
	$(COMPOSE) exec -T api ruff check src tests
	$(COMPOSE) exec -T api lint-imports

fmt: ## Format and autofix
	$(COMPOSE) exec -T api ruff format src tests
	$(COMPOSE) exec -T api ruff check --fix src tests

load-test: ## Local traffic harness against /v1/echo (N=, CONCURRENCY=)
	$(COMPOSE) exec -T api python -m loadtest.run --requests $(or $(N),2000) --concurrency $(or $(CONCURRENCY),50)

logs: ## Tail logs for all services
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps

psql: ## psql shell
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-meter} -d $${POSTGRES_DB:-meter}

redis-cli: ## redis-cli shell
	$(COMPOSE) exec redis redis-cli

shell: ## Shell in the api container
	$(COMPOSE) exec api bash

clean: ## Stop everything and drop volumes
	$(COMPOSE) down -v --remove-orphans
