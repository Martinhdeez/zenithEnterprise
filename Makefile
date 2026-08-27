.PHONY: up up-models down logs migrate revision test lint format format-check types licenses \
	web-install web-types web-test web check demo-check backup

COMPOSE := docker compose -f docker/docker-compose.yml
UV := cd backend && uv run
WEB := cd frontend && npm

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

web-install:
	$(WEB) ci

# The frontend's equivalent of `types` and `test`. Separate targets so a backend-only
# change does not pay for a node_modules install, and one `web` target so `check` has a
# single thing to call.
web-types:
	$(WEB) run lint

web-test:
	$(WEB) run test

web: web-types web-test

# What has to pass before every commit. Mirrors the CI job exactly, so a green
# `check` locally means a green pipeline.
check: lint format-check types test licenses web

# A different question from `check`, and the one to ask before a demonstration. `check` says
# the code is correct; this says the *running installation* is fit to be shown — the right
# models loaded, the proxy reaching the API, and search answering without falling back to a
# degraded path. The product degrades rather than fails by design, which is why nobody
# notices until the answers are visibly worse in front of an audience.
#
#   make demo-check EMAIL=you@example.com PASSWORD=...
demo-check:
	./scripts/demo-check.sh $(EMAIL) $(PASSWORD)

# Database and documents, verified. See docs/deployment.md.
backup:
	./scripts/backup.sh $(TO)
