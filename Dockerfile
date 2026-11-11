# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Stages:
#   builder    the core package, installed for the runtime image
#   postgres   PostgreSQL 18 server binaries with pgvector, and Python from uv,
#              for anything that builds or restores PostgreSQL working databases
#   test       the workspace with its development tools on top of postgres;
#              runs the test suite as an unprivileged user (docker compose)
#   runtime    the published image: the core package and nothing else
#
# Base images are pinned by digest; Dependabot keeps them current.

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS base
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_FROZEN=1
WORKDIR /src

FROM base AS builder
ENV UV_PROJECT_ENVIRONMENT=/opt/datadouble
COPY pyproject.toml uv.lock ./
COPY core/ core/
COPY adapters/ adapters/
RUN uv sync --no-dev --package datadouble --no-editable

FROM postgres:18-trixie@sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42 AS postgres
# pgvector because Pagila uses it: working databases need the source's
# extensions. git for the source checker's tests.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git postgresql-18-pgvector \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    UV_PYTHON=3.15 \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed
RUN uv python install 3.15
WORKDIR /src

FROM postgres AS test
# initdb refuses to run as root, so the suite runs as an ordinary user.
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin tester \
    && install --directory --owner tester --group tester /src /reports
USER tester
COPY --chown=tester:tester pyproject.toml uv.lock ./
COPY --chown=tester:tester core/pyproject.toml core/README.md core/LICENSE core/
COPY --chown=tester:tester core/src/datadouble/__init__.py core/src/datadouble/
COPY --chown=tester:tester adapters/postgres/pyproject.toml adapters/postgres/README.md adapters/postgres/LICENSE adapters/postgres/
COPY --chown=tester:tester adapters/s3/pyproject.toml adapters/s3/README.md adapters/s3/LICENSE adapters/s3/
COPY --chown=tester:tester conformance/pyproject.toml conformance/README.md conformance/LICENSE conformance/
RUN uv sync --no-install-workspace
COPY --chown=tester:tester . .
RUN uv sync
CMD ["uv", "run", "pytest", "--cov", "--cov-report=term-missing", "--cov-report=xml:/reports/coverage.xml"]

FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime
LABEL org.opencontainers.image.source=https://github.com/datadouble/datadouble \
      org.opencontainers.image.description="Realistic, safe copies of production data for development and testing" \
      org.opencontainers.image.licenses=MIT
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin datadouble
COPY --from=builder /opt/datadouble /opt/datadouble
ENV PATH=/opt/datadouble/bin:$PATH
USER 10001:10001
ENTRYPOINT ["datadouble"]
CMD ["--help"]
