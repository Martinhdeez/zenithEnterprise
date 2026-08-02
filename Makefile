.PHONY: up up-models down logs migrate revision test lint format format-check types licenses check

COMPOSE := docker compose -f docker/docker-compose.yml
UV := cd backend && uv run

up:
	$(COMPOSE) up -d db
	@echo "Postgres up. Models: make up-models"

up-models:
	$(COMPOSE) up -d tei-embed tei-rerank

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

migrate:
	$(UV) alembic upgrade head

revision:
	$(UV) alembic revision --autogenerate -m "$(m)"

test:
	$(UV) pytest

lint:
	$(UV) ruff check .

format:
	$(UV) ruff format .

format-check:
	$(UV) ruff format --check .

types:
	$(UV) pyright

licenses:
	./scripts/check-licences.sh

# What has to pass before every commit. Mirrors the CI job exactly, so a green
# `check` locally means a green pipeline.
check: lint format-check types test licenses
