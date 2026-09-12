.PHONY: install format lint types test check run smoke

install:
	uv sync --all-groups

format:
	uv run ruff format app tests

lint:
	uv run ruff format --check app tests
	uv run ruff check app tests

types:
	uv run mypy

test:
	uv run pytest --cov --cov-report=term-missing

check: format lint types test

run:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8080

smoke:
	./scripts/smoke.sh
