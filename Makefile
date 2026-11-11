# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

.PHONY: check lint lint-workflows typecheck test test-integration docs docs-serve clean

# Everything that must pass before a pull request: lint, types, unit tests.
check: lint lint-workflows typecheck test

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run python scripts/check_source.py

# GitHub workflows: actionlint for syntax and shell, zizmor for security.
lint-workflows:
	actionlint
	uvx --from zizmor==1.32.0 zizmor --offline .github/workflows

typecheck:
	uv run mypy
	uv run pyright

# Unit tests on the host, with the 100% coverage gate.
test:
	uv run pytest -m "not integration" --cov

# The whole suite, including integration tests, inside the default Compose group.
# Reports are copied out of the stopped container rather than bind-mounted.
test-integration:
	mkdir -p .reports
	docker compose up --build --abort-on-container-exit --exit-code-from test test; \
	status=$$?; \
	docker compose cp test:/reports/coverage.xml .reports/coverage.xml || true; \
	docker compose down --volumes; \
	exit $$status

docs:
	uv run --group docs zensical build --strict

docs-serve:
	uv run --group docs zensical serve

clean:
	docker compose down --volumes --remove-orphans
	rm -rf .reports site .coverage coverage.xml
