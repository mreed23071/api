# Every command CI runs is here, so "works locally" and "passes CI" mean the
# same thing. CI calls these targets rather than duplicating the commands.

.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help install lint format typecheck test test-integration test-all test-cov cov cov-all openapi openapi-check migrate revision run up down check

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Create the virtualenv from pyproject.toml
	$(UV) sync --group dev --group integration

lint:  ## Ruff check
	$(UV) run ruff check .

format:  ## Ruff format + import sort
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:  ## mypy over the application source
	$(UV) run mypy src

test:  ## Fast suite: unit + api + contract. No Docker, no network, no model.
	$(UV) run pytest -m "not integration"

test-integration:  ## Integration suite against a real pgvector container
	$(UV) run pytest -m integration

test-all:  ## Everything
	$(UV) run pytest

test-cov:  ## Fast suite with coverage as XML - what CI's "test" job runs
	$(UV) run pytest -m "not integration" --cov --cov-report=term-missing --cov-report=xml

cov:  ## Fast suite with a coverage report (terminal + HTML in htmlcov/)
	$(UV) run pytest -m "not integration" --cov --cov-report=term-missing --cov-report=html

cov-all:  ## Full suite with coverage - the number CI enforces
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=xml

openapi:  ## Regenerate openapi/<version>.json
	$(UV) run python scripts/export_openapi.py

openapi-check:  ## Fail if the committed schema is stale
	$(UV) run python scripts/export_openapi.py --check

migrate:  ## Apply migrations
	$(UV) run alembic upgrade head

revision:  ## Autogenerate a migration: make revision m="add x"
	$(UV) run alembic revision --autogenerate -m "$(m)"

seed:  ## Load the demo dataset. Idempotent - safe to run again.
	$(UV) run python scripts/seed.py

run:  ## Run the API with reload against a local database
	$(UV) run uvicorn app.main:app --reload --app-dir src

up:  ## Start the full stack
	docker compose up --build

down:  ## Stop the stack
	docker compose down

check: lint typecheck test openapi-check  ## Everything CI runs on a pull request
