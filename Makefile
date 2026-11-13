# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

.PHONY: check lint lint-workflows typecheck test test-integration test-k8s test-btrfs docs docs-serve clean

# Everything that must pass before a pull request: lint, types, unit tests.
check: lint lint-workflows typecheck test

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run python scripts/check_source.py

# GitHub workflows: actionlint for syntax and shell, zizmor for security.
lint-workflows:
	actionlint
	uv run python scripts/check_workflows.py
	uvx --from zizmor==1.32.0 zizmor --offline .github/workflows

typecheck:
	uv run mypy
	uv run pyright

# Unit tests on the host, with the 100% coverage gate over the code they can
# reach. Adapter code that needs a database is covered by test-integration.
test:
	uv run pytest -m "not integration" --cov=core/src --cov=scripts

# The whole suite, including integration tests, inside the default Compose group.
# Reports are copied out of the stopped container rather than bind-mounted.
test-integration:
	mkdir -p .reports
	docker compose up --build --abort-on-container-exit --exit-code-from test test; \
	status=$$?; \
	docker compose cp test:/reports/coverage.xml .reports/coverage.xml || true; \
	docker compose down --volumes; \
	exit $$status

# Opt-in groups that need privileged containers: k3s, and a loop-mounted btrfs.
test-k8s:
	docker compose --profile k8s up --build --abort-on-container-exit --exit-code-from test-k8s k3s test-k8s; \
	status=$$?; \
	docker compose --profile k8s down --volumes; \
	exit $$status

test-btrfs:
	docker compose --profile btrfs up --build --abort-on-container-exit --exit-code-from test-btrfs test-btrfs; \
	status=$$?; \
	docker compose --profile btrfs down --volumes; \
	exit $$status

docs:
	uv run --group docs zensical build --strict

docs-serve:
	uv run --group docs zensical serve

clean:
	docker compose down --volumes --remove-orphans
	rm -rf .reports site .coverage coverage.xml
