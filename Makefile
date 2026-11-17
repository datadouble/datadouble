# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

.PHONY: check lint lint-workflows typecheck test test-integration test-group integration clean-work test-k8s test-btrfs test-ci validate-plugin test-skills docs docs-serve clean

# Everything that must pass before a pull request: lint, types, unit tests.
check: lint lint-workflows typecheck test

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run python scripts/check_source.py

# GitHub workflows: actionlint for syntax and shell, zizmor for security; the
# workflows the GitHub Actions adapter writes (its golden files); and that the
# nightly run's generated workflows are current (scripts/nightly.py).
lint-workflows:
	actionlint
	actionlint -config-file ci/github-actions/tests/actionlint.yaml ci/github-actions/tests/golden/*/*.golden
	uv run python scripts/nightly.py check
	uv run python scripts/check_workflows.py
	uvx --from zizmor==1.32.0 zizmor --offline .github/workflows

typecheck:
	uv run mypy
	uv run pyright

# Unit tests on the host, with the 100% coverage gate over the code they can
# reach. Adapter code that needs a database is covered by test-integration.
test:
	uv run pytest -m "not integration and not ci_conformance" --cov=core/src --cov=ci/github-actions/src --cov=ci/gitlab/src --cov=ci/jenkins/src --cov=ci/kubernetes/src --cov=scripts

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

# The whole suite also runs each CI/CD system's conformance suite (test-ci),
# whose coverage joins the groups' before the combined report and its gate.
CI_SYSTEMS := github-actions gitlab jenkins kubernetes

test-integration:
	rm -f .reports/.coverage.*
	@$(MAKE) --no-print-directory integration RUN_GROUPS="$$(scripts/integration.sh groups)"
	@for system in $(CI_SYSTEMS); do \
		$(MAKE) --no-print-directory test-ci SYSTEM=$$system COVERAGE_FILE=.reports/.coverage.ci-$$system \
			PYTEST_ARGS="--cov --cov-report= --cov-fail-under=0" || exit 1; \
	done
	REPORTS=.reports COVERAGE="uv run coverage" PYTHON="uv run python" scripts/integration.sh report

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
			bash -c '/src/scripts/integration.sh test $(RUN_GROUPS)'; \
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

# The CI/CD conformance suite for one system, such as `make test-ci
# SYSTEM=github-actions`: real generated jobs, run in the system's local
# runtime (act for GitHub Actions, GitLab CE and a runner for GitLab, Jenkins
# for Jenkins, kind with Argo CD for kubernetes) with the Datadouble image
# built from this checkout. Worlds live under CI_WORK, which the Docker
# daemon must see.
CI_IMAGE ?= datadouble:ci-test
CI_WORK ?= /var/tmp

test-ci:
	@test -n "$(SYSTEM)" || { echo "name a system: make test-ci SYSTEM=github-actions"; exit 2; }
	docker build --target runtime --tag $(CI_IMAGE) .
	DATADOUBLE_TEST_CI_IMAGE=$(CI_IMAGE) DATADOUBLE_TEST_CI_WORK=$(CI_WORK) \
		$(if $(COVERAGE_FILE),COVERAGE_FILE=$(COVERAGE_FILE)) \
		uv run pytest -p no:cacheprovider -m ci_conformance ci/$(SYSTEM)/tests $(PYTEST_ARGS)

# The Claude Code plugin: its manifests and skills, and the skill scenarios
# (evals/), run by `claude plugin eval` against a copy of only the plugin's
# files, since the scenarios' sandbox reads the whole plugin directory. The
# scenarios run Claude on your own credential, and `datadouble` from this
# checkout; EVAL_ARGS adds options, such as `--case build-github --runs 1`.
validate-plugin:
	claude plugin validate . --strict

test-skills:
	copy=$$(mktemp -d /tmp/datadouble-plugin.XXXXXX); \
	cp -R .claude-plugin skills evals README.md LICENSE "$$copy"/ && \
	PATH="$(CURDIR)/.venv/bin:$$PATH" claude plugin eval "$$copy" --scaffold \
		--allow-tools Bash --trust-plugin --no-publish --ablation none \
		--output-dir $(CURDIR)/.reports/skills $(EVAL_ARGS); \
	status=$$?; rm -rf "$$copy"; exit $$status

docs:
	uv run --group docs zensical build --strict

docs-serve:
	uv run --group docs zensical serve

clean:
	docker compose down --volumes --remove-orphans
	rm -rf .reports site .coverage coverage.xml
