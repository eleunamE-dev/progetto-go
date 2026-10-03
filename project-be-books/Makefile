# Development tasks for the book review service. Run `make` to list them.

GOLANGCI_LINT_VERSION ?= v2.14.0
# golangci-lint runs in Docker so everyone uses the same version; set
# GOLANGCI_LINT=golangci-lint to use a local installation instead.
GOLANGCI_LINT ?= docker run --rm -v "$(CURDIR)":/app -w /app \
	-v bookreviews-golangci-cache:/root/.cache -v bookreviews-gomod-cache:/go/pkg/mod \
	golangci/golangci-lint:$(GOLANGCI_LINT_VERSION) golangci-lint

.DEFAULT_GOAL := help

.PHONY: help
help: ## List the available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-8s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: build
build: ## Build every command into ./bin
	go build -o bin/ ./cmd/...

.PHONY: run
run: ## Run the API on the host
	go run ./cmd/api

.PHONY: test
test: ## Run the unit tests with the race detector
	go test -race ./...

.PHONY: cover
cover: ## Run the unit tests and report coverage per function
	go test -race -coverprofile=coverage.out ./...
	go tool cover -func=coverage.out

.PHONY: lint
lint: ## Run the linters
	$(GOLANGCI_LINT) run ./...

.PHONY: fmt
fmt: ## Format the code (gofumpt + goimports)
	$(GOLANGCI_LINT) fmt ./...

.PHONY: up
up: ## Build and start the whole stack with Docker Compose
	docker compose up --build --detach

.PHONY: down
down: ## Stop the stack (data volumes are kept)
	docker compose down

.PHONY: logs
logs: ## Follow the logs of the stack
	docker compose logs --follow
