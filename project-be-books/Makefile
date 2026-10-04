.DEFAULT_GOAL := help

TEST_DATABASE_URL ?= mysql+aiomysql://root:rootpassword@localhost:3306/bookreviews_test
TEST_RABBITMQ_URL ?= amqp://user:password@localhost:5672/
MIGRATION_DATABASE_URL ?= mysql+aiomysql://migrator:migrator-password@localhost:3306/bookreviews

.PHONY: help
help: ## List the available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-9s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: install
install: ## Create the virtual environment with every dependency
	uv sync

.PHONY: hooks
hooks: ## Install the git hooks: checks on commit, tests on push
	uv run pre-commit install

.PHONY: run
run: ## Run the API on the host (needs `make migrate` first)
	uv run bookreviews-api

.PHONY: worker
worker: ## Run the enrichment worker on the host
	uv run bookreviews-worker

.PHONY: migrate
migrate: ## Apply the database migrations, as the migrator account
	DATABASE_URL=$(MIGRATION_DATABASE_URL) uv run bookreviews-migrate

.PHONY: test
test: ## Run the unit tests with coverage
	uv run pytest --cov

.PHONY: test-all
test-all: ## Run every test, using MariaDB and RabbitMQ from Docker Compose
	docker compose up --detach --wait db rabbitmq
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_RABBITMQ_URL=$(TEST_RABBITMQ_URL) uv run pytest --cov

.PHONY: openapi
openapi: ## Export the OpenAPI document to docs/openapi.json
	uv run bookreviews-openapi docs/openapi.json

.PHONY: lint
lint: ## Check formatting, lint rules and types
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy

.PHONY: fmt
fmt: ## Format the code and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

.PHONY: up
up: ## Build and start the whole stack with Docker Compose
	docker compose up --build --detach --wait

.PHONY: down
down: ## Stop the stack (data volumes are kept)
	docker compose down

.PHONY: logs
logs: ## Follow the logs of the stack
	docker compose logs --follow
