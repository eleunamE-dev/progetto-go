.DEFAULT_GOAL := help

TEST_DATABASE_URL ?= mysql+aiomysql://root:rootpassword@127.0.0.1:3306/bookreviews_test
TEST_RABBITMQ_URL ?= amqp://user:password@127.0.0.1:5672/
MIGRATION_DATABASE_URL ?= mysql+aiomysql://migrator:migrator-password@127.0.0.1:3306/bookreviews
PROMTOOL = docker run --rm --entrypoint promtool -v "$(CURDIR)/deploy/observability:/etc/prometheus" -w /etc/prometheus prom/prometheus:v3.15.0
KUBECONFORM = docker run --rm -i ghcr.io/yannh/kubeconform:v0.8.0 -strict -summary -kubernetes-version 1.34.0 -schema-location default -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

.PHONY: help
help: ## List the available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: install
install: ## Create the virtual environment with every dependency
	uv sync

.PHONY: hooks
hooks: ## Install the git hooks: checks on commit, tests on push
	uv run pre-commit install --config .pre-commit-config.yaml

.PHONY: check
check: ## Run every commit hook on all the files of the project
	uv run pre-commit run --config .pre-commit-config.yaml --all-files

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

.PHONY: e2e
e2e: ## Check a fresh Compose stack end to end (stop `make up` first: same ports)
	uv run python -m e2e.stack

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

.PHONY: observability
observability: ## Start the stack with Prometheus, Grafana and Jaeger
	docker compose -f docker-compose.yaml -f docker-compose.observability.yaml up --build --detach --wait

.PHONY: alerts
alerts: ## Check the Prometheus configuration and run the alert rule tests
	$(PROMTOOL) check config prometheus.yml
	$(PROMTOOL) test rules alerts.test.yml

.PHONY: manifests
manifests: ## Validate the Kubernetes overlays with kubeconform
	for overlay in deploy/kubernetes/overlays/*/; do \
		kubectl kustomize $$overlay | $(KUBECONFORM) - || exit 1; \
	done

.PHONY: kind-up
kind-up: ## Create a local kind cluster and deploy the local overlay on it
	bash deploy/kubernetes/local/up.sh

.PHONY: kind-verify
kind-verify: ## Check the deployment on the local kind cluster
	uv run python -m e2e.cluster

.PHONY: kind-down
kind-down: ## Delete the local kind cluster
	kind delete cluster --name bookreviews

.PHONY: down
down: ## Stop the stack (data volumes are kept)
	docker compose down

.PHONY: logs
logs: ## Follow the logs of the stack
	docker compose logs --follow
