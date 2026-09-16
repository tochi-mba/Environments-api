UV := uv

.PHONY: help install fmt lint type imports test cov check run docker smoke clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install everything
	$(UV) sync --all-groups

fmt: ## Format and auto-fix
	$(UV) run ruff format app tests
	$(UV) run ruff check --fix app tests

lint: ## Lint and check formatting (no fixes)
	$(UV) run ruff format --check app tests
	$(UV) run ruff check app tests

type: ## Strict type check
	$(UV) run mypy

imports: ## Enforce the architectural layering contracts
	$(UV) run lint-imports

test: ## Run the test suite with 100% branch coverage enforced
	$(UV) run pytest --cov --cov-report=term-missing

cov: ## Write an HTML coverage report to htmlcov/
	$(UV) run pytest --cov --cov-report=html

check: lint type imports test ## Everything CI runs. Only honest on Linux -- see CONTRIBUTING.md

run: ## Serve the API on :8008 with reload
	$(UV) run uvicorn app.main:app --host 0.0.0.0 --port 8008 --reload

# Signed-in gh fetches private client packages; with no session git fetches anonymously.
docker: ## Build the container image
	@GITHUB_TOKEN="$$(gh auth token 2>/dev/null)" docker build --secret id=github_token,env=GITHUB_TOKEN -t environments-api:local .

smoke: ## End-to-end check against a running environments-api and a running keyring
	./scripts/smoke.sh

clean: ## Remove caches and build output
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage .coverage-data htmlcov dist build
