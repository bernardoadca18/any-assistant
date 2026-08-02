# =============================================================================
# UE5 AI Assistant - Professional Makefile
# =============================================================================
# Multi-target Makefile for development, production, and maintenance operations
# Usage: make <target> [ARGS="additional args"]
# =============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Colors for output
CYAN := \033[36m
GREEN := \033[32m
YELLOW := \033[33m
RED := \033[31m
RESET := \033[0m

# Project variables
PROJECT_NAME := ue5-ai-assistant
COMPOSE_FILE := docker-compose.yml
BACKEND_SERVICE := ue5-agent
FRONTEND_SERVICE := frontend
QDRANT_SERVICE := qdrant
REDIS_SERVICE := agent-redis
POSTGRES_SERVICE := agent-postgres

# Docker Compose commands
DC := docker compose -f $(COMPOSE_FILE)
DC_BUILD := $(DC) build
DC_UP := $(DC) up -d
DC_DOWN := $(DC) down
DC_LOGS := $(DC) logs -f
DC_PS := $(DC) ps

# =============================================================================
# HELP
# =============================================================================

.PHONY: help
help: ## Show this help message
	@echo ""
	@echo "$(CYAN)$(PROJECT_NAME) - Available Commands$(RESET)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-25s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# =============================================================================
# DEVELOPMENT
# =============================================================================

.PHONY: dev
dev: ## Start all services in development mode (with hot-reload)
	@echo "$(CYAN)Starting development environment...$(RESET)"
	$(DC) --profile dev up -d
	@echo "$(GREEN)Development environment ready!$(RESET)"
	@echo "Frontend: http://localhost:5173"
	@echo "Backend:  http://localhost:8000"
	@echo "Docs:     http://localhost:8000/docs"

.PHONY: dev-backend
dev-backend: ## Start only backend services in development mode
	@echo "$(CYAN)Starting backend in development mode...$(RESET)"
	$(DC) up -d qdrant agent-redis agent-postgres
	$(DC) build --target development $(BACKEND_SERVICE)
	$(DC) up -d $(BACKEND_SERVICE)
	@echo "$(GREEN)Backend ready at http://localhost:8000$(RESET)"

.PHONY: dev-frontend
dev-frontend: ## Start only frontend in development mode (requires backend running)
	@echo "$(CYAN)Starting frontend in development mode...$(RESET)"
	$(DC) build --target development $(FRONTEND_SERVICE)
	$(DC) --profile dev-frontend up -d $(FRONTEND_SERVICE)
	@echo "$(GREEN)Frontend ready at http://localhost:5173$(RESET)"

# =============================================================================
# PRODUCTION
# =============================================================================

.PHONY: up
up: ## Start all services in production mode
	@echo "$(CYAN)Starting production environment...$(RESET)"
	$(DC_BUILD)
	$(DC_UP)
	@echo "$(GREEN)Production environment ready!$(RESET)"
	@echo "Frontend: http://localhost:3000"
	@echo "Backend:  http://localhost:8000"

.PHONY: down
down: ## Stop all services
	@echo "$(YELLOW)Stopping all services...$(RESET)"
	$(DC_DOWN)
	@echo "$(GREEN)All services stopped.$(RESET)"

.PHONY: restart
restart: down up ## Restart all services

.PHONY: rebuild
rebuild: ## Force rebuild all containers
	@echo "$(CYAN)Rebuilding all containers...$(RESET)"
	$(DC) build --no-cache
	$(DC_UP)
	@echo "$(GREEN)Rebuild complete.$(RESET)"

# =============================================================================
# LOGS & MONITORING
# =============================================================================

.PHONY: logs
logs: ## Show logs for all services
	$(DC_LOGS)

.PHONY: logs-backend
logs-backend: ## Show backend logs
	$(DC_LOGS) $(BACKEND_SERVICE)

.PHONY: logs-frontend
logs-frontend: ## Show frontend logs
	$(DC_LOGS) $(FRONTEND_SERVICE)

.PHONY: logs-qdrant
logs-qdrant: ## Show Qdrant logs
	$(DC_LOGS) $(QDRANT_SERVICE)

.PHONY: status
status: ## Show status of all services
	@echo "$(CYAN)Service Status:$(RESET)"
	$(DC_PS)

.PHONY: health
health: ## Check health of all services
	@echo "$(CYAN)Checking service health...$(RESET)"
	@curl -sf http://localhost:8000/v1/health | jq . || echo "$(RED)Backend unhealthy$(RESET)"
	@curl -sf http://localhost:3000/health > /dev/null && echo "$(GREEN)Frontend: OK$(RESET)" || echo "$(RED)Frontend unhealthy$(RESET)"

# =============================================================================
# DATABASE & STORAGE
# =============================================================================

.PHONY: db-shell
db-shell: ## Open Postgres shell
	@echo "$(CYAN)Opening Postgres shell...$(RESET)"
	$(DC) exec $(POSTGRES_SERVICE) psql -U $${AGENT_POSTGRES_USER:-ue5rag} -d $${AGENT_POSTGRES_DB:-ue5_rag_assistant}

.PHONY: db-migrate
db-migrate: ## Run database migrations
	@echo "$(CYAN)Running database migrations...$(RESET)"
	$(DC) exec $(BACKEND_SERVICE) alembic upgrade head

.PHONY: db-backup
db-backup: ## Backup Postgres database
	@echo "$(CYAN)Creating database backup...$(RESET)"
	@mkdir -p backups/postgres
	$(DC) exec $(POSTGRES_SERVICE) pg_dump -U $${AGENT_POSTGRES_USER:-ue5rag} $${AGENT_POSTGRES_DB:-ue5_rag_assistant} > backups/postgres/backup_$$(date +%Y%m%d_%H%M%S).sql
	@echo "$(GREEN)Backup created in backups/postgres/$(RESET)"

.PHONY: redis-shell
redis-shell: ## Open Redis CLI
	@echo "$(CYAN)Opening Redis CLI...$(RESET)"
	$(DC) exec $(REDIS_SERVICE) redis-cli -a $${AGENT_REDIS_PASSWORD}

.PHONY: qdrant-dashboard
qdrant-dashboard: ## Open Qdrant dashboard URL
	@echo "$(CYAN)Qdrant Dashboard: http://localhost:6333/dashboard$(RESET)"
	@open http://localhost:6333/dashboard 2>/dev/null || xdg-open http://localhost:6333/dashboard 2>/dev/null || true

# =============================================================================
# TESTING & QUALITY
# =============================================================================

.PHONY: test
test: ## Run all tests
	@echo "$(CYAN)Running tests...$(RESET)"
	$(DC) exec $(BACKEND_SERVICE) pytest -v

.PHONY: test-backend
test-backend: ## Run backend tests
	@echo "$(CYAN)Running backend tests...$(RESET)"
	$(DC) exec $(BACKEND_SERVICE) pytest app/tests -v --cov=app --cov-report=term-missing

.PHONY: test-frontend
test-frontend: ## Run frontend tests
	@echo "$(CYAN)Running frontend tests...$(RESET)"
	cd frontend && npm test

.PHONY: lint
lint: ## Run linters for all services
	@echo "$(CYAN)Running linters...$(RESET)"
	$(DC) exec $(BACKEND_SERVICE) ruff check app/
	cd frontend && npm run lint

.PHONY: format
format: ## Format code for all services
	@echo "$(CYAN)Formatting code...$(RESET)"
	$(DC) exec $(BACKEND_SERVICE) ruff format app/
	cd frontend && npm run format

# =============================================================================
# INGESTION
# =============================================================================

.PHONY: ingest
ingest: ## Run ingestion pipeline (use ARGS="folder=/path type=docs")
	@echo "$(CYAN)Running ingestion pipeline...$(RESET)"
	@$(DC) exec $(BACKEND_SERVICE) python -m app.ingestion.cli $(ARGS)

.PHONY: ingest-ue5
ingest-ue5: ## Ingest UE5 Engine documentation
	@echo "$(CYAN)Ingesting UE5 Engine documentation...$(RESET)"
	$(DC) exec $(BACKEND_SERVICE) curl -X POST http://localhost:8000/v1/ingest/process-folder \
		-H "Content-Type: application/json" \
		-d '{"folder_path": "/data/ue5_engine_root", "content_type": "source_code", "project_source": "ue5_engine"}'

.PHONY: ingest-status
ingest-status: ## Check ingestion job status
	@echo "$(CYAN)Checking ingestion status...$(RESET)"
	@curl -sf http://localhost:8000/v1/ingest/jobs | jq . || echo "$(RED)Failed to get status$(RESET)"

# =============================================================================
# CLEANUP
# =============================================================================

.PHONY: clean
clean: ## Remove stopped containers and dangling images
	@echo "$(YELLOW)Cleaning up Docker resources...$(RESET)"
	docker system prune -f
	@echo "$(GREEN)Cleanup complete.$(RESET)"

.PHONY: deep-clean
deep-clean: ## Remove all containers, volumes, and images (WARNING: destructive)
	@echo "$(RED)WARNING: This will remove all data! Press Ctrl+C to cancel...$(RESET)"
	@sleep 5
	$(DC_DOWN) -v --rmi all
	docker system prune -af --volumes
	@echo "$(GREEN)Deep cleanup complete.$(RESET)"

.PHONY: reset
reset: deep-clean up ## Reset everything to clean state (WARNING: destructive)

# =============================================================================
# UTILITIES
# =============================================================================

.PHONY: shell-backend
shell-backend: ## Open shell in backend container
	@echo "$(CYAN)Opening backend shell...$(RESET)"
	$(DC) exec $(BACKEND_SERVICE) /bin/bash

.PHONY: shell-frontend
shell-frontend: ## Open shell in frontend container
	@echo "$(CYAN)Opening frontend shell...$(RESET)"
	$(DC) exec $(FRONTEND_SERVICE) /bin/sh

.PHONY: env-setup
env-setup: ## Copy .env.example to .env if not exists
	@if [ ! -f .env ]; then \
		echo "$(CYAN)Creating .env from .env.example...$(RESET)"; \
		cp .env.example .env; \
		echo "$(GREEN).env created. Please edit with your values.$(RESET)"; \
	else \
		echo "$(YELLOW).env already exists. Skipping.$(RESET)"; \
	fi

.PHONY: deps-install
deps-install: ## Install all dependencies locally (for IDE support)
	@echo "$(CYAN)Installing backend dependencies...$(RESET)"
	pip install -r requirements.txt
	@echo "$(CYAN)Installing frontend dependencies...$(RESET)"
	cd frontend && npm install
	@echo "$(GREEN)All dependencies installed.$(RESET)"

.PHONY: update
update: ## Pull latest images and rebuild
	@echo "$(CYAN)Updating project...$(RESET)"
	git pull
	$(DC) pull
	$(DC) build --no-cache
	$(DC_UP)
	@echo "$(GREEN)Update complete.$(RESET)"

# =============================================================================
# INFO
# =============================================================================

.PHONY: info
info: ## Show project information
	@echo ""
	@echo "$(CYAN)$(PROJECT_NAME)$(RESET)"
	@echo ""
	@echo "Services:"
	@echo "  Frontend:  http://localhost:3000 (prod) / http://localhost:5173 (dev)"
	@echo "  Backend:   http://localhost:8000"
	@echo "  API Docs:  http://localhost:8000/docs"
	@echo "  Qdrant:    http://localhost:6333/dashboard"
	@echo ""
	@echo "Quick Start:"
	@echo "  1. make env-setup    # Create .env file"
	@echo "  2. make up           # Start all services"
	@echo "  3. make health       # Check service health"
	@echo ""
