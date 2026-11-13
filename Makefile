# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

.PHONY: check lint lint-workflows typecheck test test-integration test-group integration clean-work test-k8s test-btrfs docs docs-serve clean

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

# The integration suite, in groups (tests/integration-groups.txt), each run as
# the user its engine's copies are served as, with only the Compose services
# it needs. `make test-integration` runs every group and combines their
# coverage into one report with the 100% gate; `make test-group GROUP=mysql`
# runs one, as each CI job does. DOCKER=host uses the host's own Docker daemon
# instead of Docker-in-Docker (compose.host.yaml), as CI does; EXTRA_COMPOSE
# adds Compose files, such as CI's build cache (compose.cache.yaml). Reports
# are copied out of the stopped test container rather than bind-mounted.
DOCKER ?= dind
HOST_WORK ?= /var/tmp/datadouble-work
EXTRA_COMPOSE ?=
COMPOSE := docker compose -f compose.yaml$(if $(filter host,$(DOCKER)), -f compose.host.yaml)$(foreach file,$(EXTRA_COMPOSE), -f $(file))
DAEMON := $(if $(filter host,$(DOCKER)),,docker)

test-integration:
	@$(MAKE) --no-print-directory integration RUN_GROUPS="$$(scripts/integration.sh groups)" REPORT=yes

test-group:
	@test -n "$(GROUP)" || { echo "name a group: make test-group GROUP=<$$(scripts/integration.sh groups | tr '\n' '|')>"; exit 2; }
	@$(MAKE) --no-print-directory integration RUN_GROUPS="$(GROUP)"

integration:
	mkdir -p .reports
	$(if $(filter host,$(DOCKER)),install -d -m 1777 $(HOST_WORK))
	DATADOUBLE_HOST_WORK=$(HOST_WORK) $(COMPOSE) build test
	$(if $(filter host,$(DOCKER)),$(MAKE) --no-print-directory clean-work)
	DATADOUBLE_HOST_WORK=$(HOST_WORK) $(COMPOSE) up -d --wait $(DAEMON) $$(scripts/integration.sh services $(RUN_GROUPS)); \
	status=$$?; \
	if [ $$status -eq 0 ]; then \
		DATADOUBLE_HOST_WORK=$(HOST_WORK) $(COMPOSE) run --name $${COMPOSE_PROJECT_NAME:-datadouble}-tests --no-deps test \
			bash -c '/src/scripts/integration.sh test $(RUN_GROUPS); status=$$?; \
				$(if $(REPORT),/src/scripts/integration.sh report || status=1;) exit $$status'; \
		status=$$?; \
		docker cp $${COMPOSE_PROJECT_NAME:-datadouble}-tests:/reports/. .reports/ || true; \
		docker rm -f $${COMPOSE_PROJECT_NAME:-datadouble}-tests > /dev/null; \
	fi; \
	DATADOUBLE_HOST_WORK=$(HOST_WORK) $(COMPOSE) down --volumes; \
	exit $$status

# Empty HOST_WORK, where DOCKER=host runs keep temporary files. A failed
# test's directory stays there to be read, until this or the next run; its
# files belong to the engines' users, so the test container removes them as
# root. (Docker-in-Docker runs keep theirs in a volume each run removes.)
clean-work:
	@test ! -d $(HOST_WORK) || DATADOUBLE_HOST_WORK=$(HOST_WORK) \
		docker compose -f compose.yaml -f compose.host.yaml run --rm --no-deps test \
		find $(HOST_WORK) -mindepth 1 -delete

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
